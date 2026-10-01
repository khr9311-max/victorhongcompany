"""전략 개선 후보(전략 연구원 주간 검토 또는 사용자 입력).

후보는 파라미터 변경만 담는 별도 설정이다. 실거래 전략을 자동으로 덮어쓰지 않으며, 임의 생성 코드를 실행하지 않는다.
사용자는 실험(백테스트) 결과·변경 이유·되돌리기 방법을 보고 승격한다. 승격은 새 설정 버전으로 기록되고 되돌릴 수 있다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from aifund.config.settings import MeanRevParams, TrendParams
from aifund.core.ids import new_id
from aifund.core.money import D
from aifund.core.timeutil import to_iso
from aifund.db.database import dumps, loads
from aifund.evaluation.backtest import run_backtest, side_fee_rate
from aifund.evaluation.metrics import strategy_metrics_payload
from aifund.service.context import AppContext
from aifund.strategies import mean_reversion, trend  # noqa: F401  (전략 등록)
from aifund.strategies.base import REGISTRY

log = logging.getLogger(__name__)

PARAM_MODELS = {"trend_sma": TrendParams, "mean_reversion": MeanRevParams}


def param_bounds() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for sid, model in PARAM_MODELS.items():
        schema = model.model_json_schema()
        out[sid] = {k: {kk: vv for kk, vv in v.items() if kk in ("minimum", "maximum", "type", "default", "anyOf")}
                    for k, v in schema["properties"].items()}
    return out


def current_params(ctx: AppContext) -> dict[str, Any]:
    st = ctx.settings.strategies
    return {"trend_sma": TrendParams.model_validate(st.trend_sma.params).model_dump(mode="json"),
            "mean_reversion": MeanRevParams.model_validate(st.mean_reversion.params).model_dump(mode="json")}


def create_candidate(ctx: AppContext, strategy_id: str, changes: dict[str, Any], rationale: str, source: str,
                     report_id: str | None = None) -> str:
    if strategy_id not in PARAM_MODELS:
        raise ValueError(f"알 수 없는 전략 {strategy_id}")
    base = current_params(ctx)[strategy_id]
    merged = {**base, **changes}
    try:
        validated = PARAM_MODELS[strategy_id].model_validate(merged).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError(f"허용 범위를 벗어난 파라미터: {exc}") from exc
    cid = new_id("cand")
    ctx.db.execute(
        "INSERT INTO strategy_candidates(candidate_id, created_at, source, strategy_id, params_json, base_params_json, rationale, "
        "ai_report_id, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, to_iso(ctx.clock.now()), source, strategy_id, dumps(validated), dumps(base), rationale, report_id, "proposed"),
    )
    return cid


async def run_weekly_review(ctx: AppContext) -> str | None:
    metrics = strategy_metrics_payload(ctx)
    rid = await ctx.ai.strategy_review(metrics, current_params(ctx), param_bounds())
    if rid is None:
        return None
    row = ctx.db.query_one("SELECT valid, report_json FROM ai_reports WHERE report_id=?", (rid,))
    if row is None or not row["valid"]:
        return rid
    report = loads(row["report_json"])["report"]
    for idea in report.get("improvement_ideas", []):
        changes = {c["name"]: c["value"] for c in idea["changes"]}
        try:
            create_candidate(ctx, idea["strategy_id"], changes, idea["rationale"], "ai_weekly", rid)
        except ValueError as exc:
            log.warning("AI 개선안 무시(범위 밖): %s", exc)
    return rid


def backtest_candidate(ctx: AppContext, candidate_id: str, market: str = "crypto", split_at: datetime | None = None) -> dict[str, Any]:
    row = ctx.db.query_one("SELECT * FROM strategy_candidates WHERE candidate_id=?", (candidate_id,))
    if row is None:
        raise LookupError(candidate_id)
    s = ctx.settings
    ms = s.markets[market]  # type: ignore[index]
    candles = {}
    insts = {}
    for sym in ms.instruments:
        iid = f"{market}:{sym}"
        inst = ctx.market_store.instrument(iid)
        cs = ctx.market_store.closed_candles(iid, ms.candle, ctx.clock.now(), 5000)
        if inst and cs:
            candles[iid], insts[iid] = cs, inst
    if not candles:
        raise RuntimeError("저장된 캔들이 없습니다. `aifund backtest --fetch`로 먼저 수집하세요.")
    cap = ms.allocation_krw * s.strategies.sleeves_for(market).get(row["strategy_id"], D("0.5"))
    if next(iter(insts.values())).quote_ccy != "KRW":
        fx = ctx.fx.status(s.risk.max_fx_age_hours)
        if not fx.fresh or fx.rate is None:
            raise RuntimeError(f"환율이 없어 외화 자본을 계산할 수 없습니다: {fx.reason}")
        cap = cap / fx.rate.rate
    fee = side_fee_rate(s.execution.paper, market)
    base = REGISTRY[row["strategy_id"]](loads(row["base_params_json"]))
    cand = REGISTRY[row["strategy_id"]](loads(row["params_json"]))
    result = {
        "base": run_backtest(base, candles, insts, capital=cap, fee_rate=fee, slippage_bps=s.execution.paper.slippage_bps, split_at=split_at),
        "candidate": run_backtest(cand, candles, insts, capital=cap, fee_rate=fee, slippage_bps=s.execution.paper.slippage_bps,
                                  split_at=split_at),
    }
    ctx.db.execute("UPDATE strategy_candidates SET status='backtested', backtest_json=? WHERE candidate_id=?",
                   (dumps(result), candidate_id))
    return result


def promote(ctx: AppContext, candidate_id: str, actor: str) -> int:
    row = ctx.db.query_one("SELECT * FROM strategy_candidates WHERE candidate_id=?", (candidate_id,))
    if row is None or row["status"] not in ("proposed", "backtested"):
        raise ValueError("승격 가능한 후보가 아닙니다")
    prev_version = ctx.settings_version
    data = ctx.settings.model_dump(mode="json")
    data["strategies"][row["strategy_id"]]["params"] = loads(row["params_json"])
    from aifund.config.settings import Settings

    version = ctx.store_settings.save(Settings.model_validate(data), "candidate_promotion",
                                      f"후보 {candidate_id} 승격({actor}): {row['rationale'][:100]}")
    ctx.db.execute("UPDATE strategy_candidates SET status='promoted', promoted_at=?, promoted_settings_version=?, "
                   "previous_settings_version=? WHERE candidate_id=?", (to_iso(ctx.clock.now()), version, prev_version, candidate_id))
    ctx.reload_settings()
    return version


def rollback(ctx: AppContext, candidate_id: str, actor: str) -> int:
    row = ctx.db.query_one("SELECT * FROM strategy_candidates WHERE candidate_id=?", (candidate_id,))
    if row is None or row["status"] != "promoted":
        raise ValueError("되돌릴 수 있는 후보가 아닙니다")
    data = ctx.settings.model_dump(mode="json")
    data["strategies"][row["strategy_id"]]["params"] = loads(row["base_params_json"])
    from aifund.config.settings import Settings

    version = ctx.store_settings.save(Settings.model_validate(data), "candidate_rollback", f"후보 {candidate_id} 되돌리기({actor})")
    ctx.db.execute("UPDATE strategy_candidates SET status='rolled_back' WHERE candidate_id=?", (candidate_id,))
    ctx.reload_settings()
    return version

