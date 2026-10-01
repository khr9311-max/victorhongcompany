"""전략 연구소 변형의 운용 연결: 모델 상태 → 신호, 순환 월말 재조정, 설정 검증, 사이클·대시보드."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aifund.config.settings import Settings, StrategySettings
from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock
from aifund.data.collector import InstrumentSnap, Snapshot
from aifund.domain.models import Action, Candle
from aifund.service.context import build_context
from aifund.service.cycle import DecisionCycle
from aifund.strategies.base import PositionView, StrategyContext
from aifund.strategies.lab import lab_class, lab_strategies, month_end_index, strategy_label, strategy_version
from helpers import inst
from test_lab import at_support, line

PROFILE = Path(__file__).resolve().parents[1] / "config" / "paper.toml"
D0 = datetime(2025, 1, 1, tzinfo=UTC)
FLAT = PositionView(Decimal(0), Decimal(0), None)


def candles(iid: str, rows: list[tuple[float, float, float, float]], start: datetime = D0) -> list[Candle]:
    return [Candle(iid, "1d", start + timedelta(days=i), start + timedelta(days=i, hours=6), Decimal(str(o)), Decimal(str(h)),
                   Decimal(str(lo)), Decimal(str(c)), Decimal(1)) for i, (o, h, lo, c) in enumerate(rows)]


def snap(items: dict[str, list[Candle]], market: str = "kr_stock") -> Snapshot:
    its = {iid: InstrumentSnap(inst(iid.split(":", 1)[1], market, "KRW", "krx", Decimal(1), Decimal(1)), cs, None)
           for iid, cs in items.items()}
    last = max(cs[-1].close_time for cs in items.values())
    return Snapshot("snap-test", market, last, "1d", last, its, "test", False, True, "")


def run(strategy, sn: Snapshot, positions: dict[str, PositionView] | None = None, now: datetime | None = None):
    return {s.instrument_id: s for s in strategy.evaluate(StrategyContext(sn, positions or {}, now or sn.created_at))}


def kangaroo_rows(extra: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """앞에 하락 15봉을 붙여 60봉을 넘긴 지지 구간 + 캥거루 꼬리(64번) + 뒤 봉들."""
    head = [(c + 0.5, c + 1.0, c - 0.5, c) for c in line(136, 121, 15)]
    b = at_support((102.6, 103.2, 97.0, 103.0))
    return head + list(zip(b.o, b.h, b.l, b.c)) + extra


def test_timing_strategy_follows_model_state():
    strat = lab_class("kangaroo_tail.zone")()
    iid = "kr_stock:TEST"
    # 65번 봉 종가가 진입가(꼬리 봉 고가 위)를 넘음 → 마지막 종가에 진입 결정 → 매수
    entry = kangaroo_rows([(103.0, 104.5, 102.9, 104.2)])
    sig = run(strat, snap({iid: candles(iid, entry)}))[iid]
    assert sig.action == Action.BUY and sig.target_weight == 1 and "진입" in sig.rationale
    held = PositionView(Decimal(1), Decimal(104), D0)
    assert run(strat, snap({iid: candles(iid, entry)}), {iid: held})[iid].action == Action.HOLD
    # 보유 중(모델도 보유) → 유지. 110 구간은 위험(약 7.4)보다 가까워 목표는 위험의 2배(약 119) → 닿으면 매도
    holding = entry + [(104.2, 105.0, 104.0, 104.8)]
    assert run(strat, snap({iid: candles(iid, holding)}), {iid: held})[iid].action == Action.HOLD
    # 모델은 보유 중인데 실제로 없으면(진입을 놓침) 따라 사지 않는다
    assert run(strat, snap({iid: candles(iid, holding)}))[iid].action == Action.WAIT
    target = holding + [(104.8, 120.0, 104.5, 118.0)]
    sig = run(strat, snap({iid: candles(iid, target)}), {iid: held})[iid]
    assert sig.action == Action.SELL and "목표 구간" in sig.rationale
    # 모델이 비어 있는데 보유가 남아 있으면(매도 실패 등) 정리한다
    after = target + [(118.0, 118.5, 117.5, 118.2)]
    assert run(strat, snap({iid: candles(iid, after)}), {iid: held})[iid].action == Action.SELL
    assert run(strat, snap({iid: candles(iid, after)}))[iid].action == Action.WAIT


def test_trend_strategy_needs_a_year_of_bars():
    strat = lab_class("high_52w.atr_trail")()
    iid = "kr_stock:TEST"
    short = [(100.0, 101.0, 99.0, 100.0)] * 200
    sig = run(strat, snap({iid: candles(iid, short)}))[iid]
    assert sig.action == Action.WAIT and "데이터 부족" in sig.rationale  # 52주 = 일봉 252봉 이상 필요
    flat = [(c, c + 0.5, c - 0.5, c) for c in (100.0 + (i % 5) * 0.2 for i in range(300))]
    up = flat + [(101.0, 102.5, 100.8, 102.0)]  # 300번 봉에서 1년 최고 종가 돌파 → 다음 봉 시가 진입
    sig = run(strat, snap({iid: candles(iid, up)}))[iid]
    assert sig.action == Action.BUY and "다음 봉 시가" in sig.rationale


def rotation_snapshot(end: datetime) -> Snapshot:
    days = (end - D0).days + 1
    paths = {"kr_stock:UP": [100 * 1.003 ** i for i in range(days)], "kr_stock:FLAT": [100.0] * days,
             "kr_stock:DOWN": [100 * 0.999 ** i for i in range(days)], "kr_stock:MID": [100 * 1.001 ** i for i in range(days)]}
    return snap({iid: candles(iid, [(c, c * 1.001, c * 0.999, c) for c in cs]) for iid, cs in paths.items()})


def test_rotation_rebalances_on_month_end_only():
    strat = lab_class("rs_top")()
    sn = rotation_snapshot(datetime(2026, 6, 30, tzinfo=UTC))  # 마지막 완성봉 = 6월 마지막 날
    first_july = datetime(2026, 7, 1, 1, tzinfo=UTC)
    assert month_end_index(sn.items["kr_stock:UP"].candles, first_july) == len(sn.items["kr_stock:UP"].candles) - 1
    held = PositionView(Decimal(1), Decimal(100), D0)
    sigs = run(strat, sn, {"kr_stock:UP": held, "kr_stock:FLAT": held}, now=first_july)
    assert sigs["kr_stock:UP"].action == Action.BUY and sigs["kr_stock:UP"].target_weight == 1  # 4종목의 1/4 = 1자리
    assert sigs["kr_stock:FLAT"].action == Action.SELL and sigs["kr_stock:DOWN"].action == Action.WAIT
    mid_month = rotation_snapshot(datetime(2026, 7, 14, tzinfo=UTC))
    later = datetime(2026, 7, 15, 1, tzinfo=UTC)
    assert run(strat, mid_month, {"kr_stock:UP": held}, now=later)["kr_stock:UP"].action == Action.HOLD  # 달 중간: 유지
    assert run(strat, mid_month, now=later)["kr_stock:UP"].action == Action.BUY  # 선정됐는데 못 산 종목은 산다
    dual = lab_class("dual_momentum")()
    falling = snap({f"kr_stock:D{k}": candles(f"kr_stock:D{k}", [(c, c, c, c) for c in [100 * (0.999 - k * 0.001) ** i
                                                                                      for i in range(560)]])
                    for k in range(2)})
    sigs = run(dual, falling, now=falling.created_at + timedelta(days=40))
    assert all(s.action == Action.WAIT for s in sigs.values())  # 절대 모멘텀이 음수면 현금


def test_settings_and_labels():
    st = StrategySettings.model_validate({"lab": {"rs_top": {"markets": ["kr_stock"]}, "trendy_kangaroo.split": {}},
                                          "market_sleeves": {"kr_stock": {"lab:rs_top": "0.5", "trend_sma": "0.5"}}})
    assert [s.strategy_id for s in lab_strategies(st, "kr_stock")] == ["lab:rs_top", "lab:trendy_kangaroo.split"]
    assert [s.strategy_id for s in lab_strategies(st, "us_stock")] == ["lab:trendy_kangaroo.split"]
    assert st.sleeves_for("kr_stock")["lab:rs_top"] == Decimal("0.5") and st.sleeves_for("crypto") == st.sleeves
    for bad in ({"lab": {"nope": {}}}, {"lab": {"big_belt.zone": {"markets": ["crypto"]}}},
                {"market_sleeves": {"kr_stock": {"a": "0.7", "b": "0.4"}}}):
        with pytest.raises(ValueError):
            StrategySettings.model_validate(bad)
    assert strategy_version("lab:rs_top") and strategy_version("trend_sma") == "1.0"
    assert strategy_label("lab:strict:kangaroo_tail.zone") == "연구소 [엄격] 캥거루 꼬리·구간 청산"
    assert Settings().strategies.lab == {}  # 기본은 연구소 전략 없음(운용 설정이 바뀌지 않음)


def test_baseline_tops_up_only_when_paper_capital_grows(home):
    from aifund.service.runtime import Runtime

    cfg = home / "config" / "paper.toml"
    # 데모 호가는 잔량이 아주 작아 1호가 잔량의 절반씩이면 몫이 차는 데 수십 주기가 걸린다 → 시험에서는 한 번에 체결
    text = PROFILE.read_text(encoding="utf-8").replace('max_fill_fraction = "0.5"', 'max_fill_fraction = "1000"')
    cfg.write_text(text, encoding="utf-8")
    paths = mode_paths("offline_demo", home)
    clock = ManualClock(datetime(2026, 9, 29, 14, 0, tzinfo=UTC))

    def basis(ctx):  # noqa: ANN001
        return {p.instrument_id: p.cost_basis for p in ctx.ledger.positions("baseline_bh") if p.qty > 0}

    def settle(ctx, rounds: int = 6):  # noqa: ANN001
        """몫이 찰 때까지 주기를 반복한다(남은 주문은 유효시간 뒤 취소). 같은 값이 두 번 나오면 끝."""
        async def one():
            await DecisionCycle(ctx).run("crypto", trigger="manual")
            ctx.clock.advance(30)
            await Runtime(ctx).poll_quotes()
            for ex in ctx.all_executors():
                await ex.poll()
            ctx.clock.advance(130)
            for ex in ctx.all_executors():
                await ex.poll()
        last = None
        for _ in range(rounds):
            asyncio.run(one())
            now = basis(ctx)
            if now == last:
                return now
            last = now
        raise AssertionError("기준선 매수가 끝나지 않음")

    ctx = build_context(paths, config_path=cfg, clock=clock)
    first = settle(ctx)
    per = Decimal(1000000) * Decimal("0.98") / 3
    assert len(first) == 3 and all(per * Decimal("0.9") <= v <= per * Decimal("1.01") for v in first.values())
    text = cfg.read_text(encoding="utf-8")
    for a, b in (('principal_cap_krw = "38000000"', 'principal_cap_krw = "39000000"'),
                 ('gross_exposure_cap_krw = "38000000"', 'gross_exposure_cap_krw = "39000000"'),
                 ('allocation_krw = "1000000"', 'allocation_krw = "2000000"')):  # 코인 배정 100만 → 200만
        assert a in text
        text = text.replace(a, b, 1)
    cfg.write_text(text, encoding="utf-8")
    grown = build_context(paths, config_path=cfg, clock=clock)
    assert grown.ledger.principal("baseline_bh") == 39000000
    second = settle(grown)
    assert all(v >= per * 2 * Decimal("0.9") for v in second.values())  # 늘어난 몫만큼 같은 비중으로 더 샀다


def test_cycle_runs_lab_strategies_and_backfills_history(home):
    cfg = home / "config" / "paper.toml"
    text = PROFILE.read_text(encoding="utf-8")
    cfg.write_text(text, encoding="utf-8")
    ctx = build_context(mode_paths("offline_demo", home), config_path=cfg,
                        clock=ManualClock(datetime(2026, 9, 29, 1, 0, tzinfo=UTC)))
    s = ctx.settings
    assert s.markets["kr_stock"].history_bars and s.strategies.lab  # 모의 설정은 연구소 전략을 켠다
    cycle = DecisionCycle(ctx)
    res = asyncio.run(cycle.run("kr_stock"))
    assert res.status == "done"
    rows = ctx.db.query("SELECT strategy_id, strategy_version, action, rationale FROM signals WHERE cycle_id=?", (res.cycle_id,))
    lab = [r for r in rows if r["strategy_id"].startswith("lab:")]
    assert {r["strategy_id"] for r in lab} == {f"lab:{k}" for k, t in s.strategies.lab.items() if "kr_stock" in t.markets}
    assert all("데이터 부족" not in r["rationale"] for r in lab)  # 과거 캔들을 채워 52주·12개월 판단이 된다
    snap_ = cycle.last_snapshots["kr_stock"]
    assert all(len(it.candles) >= 300 for it in snap_.items.values())
    assert any("과거 캔들 보강" in n for it in snap_.items.values() for n in it.notes)
    payload = cycle.signals_payload(snap_)  # AI 연구팀 입력: 출력 형식이 봇 A·B만 다루므로 연구소 신호는 빼야 한다
    assert payload and {p["strategy_id"] for p in payload} == {"trend_sma", "mean_reversion"}
    # 모의 설정은 국내·미국주식에서 연구소 전략에 슬리브를 준다(실제 모의 주문). 코인은 기본 슬리브 그대로
    kr = s.strategies.sleeves_for("kr_stock")
    assert sum(kr.values()) == 1 and all(kr[f"lab:{k}"] > 0 for k in s.strategies.lab)
    assert s.strategies.sleeves_for("crypto") == s.strategies.sleeves
    props = ctx.db.query("SELECT strategy_id, status FROM proposals WHERE cycle_id=? AND strategy_id LIKE 'lab:%'", (res.cycle_id,))
    assert props  # 연구소 전략 신호가 다른 전략과 같은 조정·위험 검사 경로로 들어간다
    from fastapi.testclient import TestClient

    from aifund.web.app import create_app

    page = TestClient(create_app(ctx, None), base_url="http://127.0.0.1:8765").get("/strategies")
    assert page.status_code == 200 and "연구소" in page.text
