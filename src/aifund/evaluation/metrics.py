"""성과 지표(실시간 사전 기록 기반).

- 거래비용 차감 손익 = 평가자산 - 원금(수수료는 이미 현금에서 차감됨). 비용 전 손익 = 차감 손익 + 수수료.
- AI 비용 포함 손익 = 거래비용 차감 손익 - (해당 설정이 사용한 AI 비용). 공유 AI 비용은 쪼개지 않고 사용한 설정마다 전액 반영.
- 데이터가 부족하면 '데이터 부족'으로 표시한다. 승률·연환산 수익률은 계산하지 않는다.
- 비교 구간·노출이 다르면 우열을 단정하지 않는다(화면에 차이를 표시).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from aifund.core.money import D, ZERO
from aifund.core.timeutil import parse_iso
from aifund.ledger.valuation import value_book
from aifund.service.context import AppContext

MIN_DAYS = 14
MIN_TRADES = 10


@dataclass
class BookMetrics:
    book_id: str
    kind: str
    setting: str
    virtual: bool
    principal: Decimal
    equity: Decimal
    pnl_net: Decimal
    fees: Decimal
    pnl_gross: Decimal
    ai_cost: Decimal
    pnl_after_ai: Decimal
    return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    turnover: Decimal | None
    trades: int
    avg_exposure_pct: Decimal | None
    faults: int
    days: float
    started_at: datetime | None
    sufficient: bool
    stale: list[str] = field(default_factory=list)
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)
    exposure: dict[str, Any] = field(default_factory=dict)


def book_metrics(ctx: AppContext, book_id: str) -> BookMetrics:
    db = ctx.db
    book = ctx.ledger.book(book_id)
    assert book is not None
    setting = ctx.book_setting(book_id)
    val = value_book(db, ctx.ledger, book_id, ctx.price, ctx.market_store.instrument, ctx.fx, ctx.settings.risk)
    principal = ctx.ledger.principal(book_id)
    fees = sum((D(r["delta"]) for r in db.query("SELECT delta FROM ledger_entries WHERE book_id=? AND kind='fee'", (book_id,))), ZERO)
    fees = -fees
    pnl_net = val.equity_krw - principal
    ai_cost = ctx.ai_cost_for_setting(setting) if book["kind"] != "baseline" else ZERO
    snaps = db.query("SELECT ts, equity_krw, positions_krw FROM equity_snapshots WHERE book_id=? AND stale=0 ORDER BY id", (book_id,))
    peak = None
    mdd = ZERO
    exp_sum = ZERO
    for s in snaps:
        eq = D(s["equity_krw"])
        peak = eq if peak is None or eq > peak else peak
        if peak and peak > 0:
            mdd = max(mdd, (peak - eq) / peak * 100)
        if eq > 0:
            exp_sum += D(s["positions_krw"]) / eq * 100
    started = parse_iso(book["created_at"])
    days = (ctx.clock.now() - started).total_seconds() / 86400 if started else 0.0
    trades = int(db.scalar("SELECT COUNT(*) FROM fills f JOIN orders o ON o.order_id=f.order_id WHERE o.book_id=?", (book_id,)) or 0)
    traded = sum((D(r["qty"]) * D(r["price"]) for r in db.query(
        "SELECT f.qty, f.price FROM fills f JOIN orders o ON o.order_id=f.order_id WHERE o.book_id=?", (book_id,))), ZERO)
    avg_eq = sum((D(s["equity_krw"]) for s in snaps), ZERO) / len(snaps) if snaps else None
    turnover = (traded / avg_eq) if avg_eq else None
    faults = int(db.scalar("SELECT COUNT(*) FROM incidents WHERE book_id=? OR account_id=?", (book_id, book["account_id"])) or 0)
    trade_counts: dict[str, int] = {}
    for r in db.query("SELECT strategy_id, COUNT(*) FROM ledger_entries WHERE book_id=? AND kind='fill' AND asset NOT IN ('KRW','USD') "
                      "GROUP BY strategy_id", (book_id,)):
        trade_counts[r[0]] = int(r[1])
    by_strategy = {
        sid: {**{k: v for k, v in st.items()}, "trades": trade_counts.get(sid, 0),
              "sleeve": str(ctx.settings.strategies.sleeves.get(sid, ZERO))}
        for sid, st in val.by_strategy.items()
    }
    return BookMetrics(
        book_id=book_id, kind=book["kind"], setting=setting, virtual=bool(book["virtual"]), principal=principal,
        equity=val.equity_krw, pnl_net=pnl_net, fees=fees, pnl_gross=pnl_net + fees, ai_cost=ai_cost, pnl_after_ai=pnl_net - ai_cost,
        return_pct=(pnl_net / principal * 100) if principal > 0 else None, max_drawdown_pct=mdd if snaps else None,
        turnover=turnover, trades=trades, avg_exposure_pct=(exp_sum / len(snaps)) if snaps else None, faults=faults, days=days,
        started_at=started, sufficient=days >= MIN_DAYS and trades >= MIN_TRADES, stale=val.stale, by_strategy=by_strategy,
        exposure={"instrument": val.by_instrument, "market": val.by_market, "class": val.by_class,
                  "gross_krw": val.exposure_krw, "reserved_buy_krw": val.reserved_buy_krw},
    )


def comparison(ctx: AppContext) -> dict[str, Any]:
    books = [book_metrics(ctx, b) for b in ctx.book_ids()]
    notes: list[str] = []
    starts = {m.started_at for m in books if m.started_at}
    if len({s.replace(microsecond=0, second=0) for s in starts if s}) > 1:
        notes.append("장부별 시작 시각이 달라 비교 구간이 다릅니다.")
    exps = [m.avg_exposure_pct for m in books if m.kind == "shadow" and m.avg_exposure_pct is not None]
    if exps and max(exps) - min(exps) > 20:
        notes.append("A/B/C 평균 노출 차이가 20%p 이상: 수익 차이는 위험 노출 차이일 수 있어 우열을 단정할 수 없습니다.")
    if not all(m.sufficient for m in books if m.kind == "shadow"):
        notes.append(f"데이터 부족(기간 {MIN_DAYS}일·거래 {MIN_TRADES}건 미만): 성과 차이를 해석하지 마세요.")
    notes.append("과거 구간 평가에서는 현재 AI가 과거 결과를 이미 알고 있을 수 있어(학습정보 누출) AI 설정 백테스트를 하지 않습니다. "
                 "A/B/C 비교는 실시간 사전 기록으로만 합니다.")
    return {"books": books, "notes": notes}


def strategy_metrics_payload(ctx: AppContext) -> list[dict[str, Any]]:
    """주간 AI 전략 검토 입력(metric:* id로 인용 가능)."""
    out = []
    for b in ctx.book_ids():
        m = book_metrics(ctx, b)
        for sid, st in m.by_strategy.items():
            out.append({"id": f"metric:{b}:{sid}", "book": b, "setting": m.setting, "strategy_id": sid,
                        "realized_krw": str(st.get("realized", ZERO)), "unrealized_krw": str(st.get("unrealized", ZERO)),
                        "fees_krw": str(st.get("fees", ZERO)), "trades": st.get("trades", 0), "days": round(m.days, 1),
                        "sufficient": m.sufficient})
    return out
