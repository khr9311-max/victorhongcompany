"""검증 9: 정지 버튼이 설명된 동작만 수행하고 재시작 뒤 정지를 유지하는가."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from aifund.control import actions
from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock
from aifund.service.context import build_context
from aifund.service.simulate import simulate


@pytest.fixture()
def demo_ctx(home):
    clock = ManualClock(datetime.now(UTC) - timedelta(hours=60))
    ctx = build_context(mode_paths("offline_demo", home).ensure(), clock=clock)
    asyncio.run(simulate(ctx, 48, ai=False))
    return ctx


def _op_qty(ctx):
    return {p.instrument_id: p.qty for p in ctx.ledger.positions("operating")}


def test_halt_persists_across_restart_and_blocks_only_buys(demo_ctx, home):
    ctx = demo_ctx
    actions.halt(ctx, "all", "테스트 중지", "test")
    before = _op_qty(ctx)
    ctx.db.close()
    ctx2 = build_context(mode_paths("offline_demo", home), clock=ctx.clock)
    assert ctx2.flags.halted("crypto") is not None  # 재시작 후에도 유지
    orders_before = ctx2.db.scalar("SELECT COUNT(*) FROM orders WHERE book_id='operating' AND side='buy'")
    asyncio.run(simulate(ctx2, 24, ai=False))
    orders_after = ctx2.db.scalar("SELECT COUNT(*) FROM orders WHERE book_id='operating' AND side='buy'")
    assert orders_after == orders_before  # 중지 중 운용 장부 신규 매수 없음
    rej = ctx2.db.query("SELECT risk_reasons_json FROM intents WHERE book_id='operating' AND side='buy' AND status='rejected'")
    assert all("신규 매수 중지" in r[0] for r in rej)
    # 매도(위험 감소)는 막지 않는다 → 보유가 있었다면 줄 수는 있어도 늘지는 않는다
    after = _op_qty(ctx2)
    for iid, q in after.items():
        assert q <= before.get(iid, Decimal(0))
    assert "재개" in actions.resume(ctx2, "all", "test")
    assert ctx2.flags.halted("crypto") is None


def test_cancel_open_only_cancels(demo_ctx):
    ctx = demo_ctx
    ex = ctx.markets["crypto"].operating_executor
    inst = ctx.market_store.instrument("crypto:KRW-BTC")
    q = ctx.market_store.latest_quote(inst.instrument_id)
    from aifund.domain.models import Side
    from aifund.execution.executor import OrderIntent

    ctx.db.execute("INSERT INTO intents(intent_id, book_id, instrument_id, side, qty, ref_price, notional_krw, risk_increasing, purpose, "
                   "status, allocations_json, created_at) VALUES ('i-c','operating','crypto:KRW-BTC','buy','1','1','1',1,'rebalance',"
                   "'approved','[]','x')")
    far = (q.bid * Decimal("0.5")).quantize(Decimal(1))
    far = far - far % inst.tick_for(far)
    oid, st = asyncio.run(ex.execute(OrderIntent("i-c", "operating", ex.account_id, "crypto", inst, Side.BUY, Decimal(1), far, far,
                                                 [("trend_sma", Decimal(1))], True)))
    assert st == "submitted"
    before = _op_qty(ctx)
    res = asyncio.run(actions.cancel_open(ctx, "crypto", "test"))
    assert any(r[0] == oid for r in res)
    assert ctx.db.query_one("SELECT status FROM orders WHERE order_id=?", (oid,))["status"] in ("canceled", "cancel_pending")
    assert _op_qty(ctx) == before  # 보유분 변화 없음
    assert ctx.flags.halted("crypto") is None  # 미체결 취소는 매수 중지를 켜지 않음


def _ensure_position(ctx) -> None:
    """운용 장부에 보유분을 결정적으로 만든다(모의 매수 → 이후 시세로 체결)."""
    from aifund.domain.models import Side
    from aifund.execution.executor import OrderIntent

    ex = ctx.markets["crypto"].operating_executor
    inst = ctx.market_store.instrument("crypto:KRW-ETH")
    q = ctx.market_store.save_quotes(asyncio.run(ctx.markets["crypto"].data.quotes([inst])))[0]
    qty = Decimal("2")
    price = q.ask + inst.tick_for(q.ask) * 5
    ctx.db.execute("INSERT INTO intents(intent_id, book_id, instrument_id, side, qty, ref_price, notional_krw, risk_increasing, purpose, "
                   "status, allocations_json, created_at) VALUES ('i-liq','operating','crypto:KRW-ETH','buy','2','1','1',1,"
                   "'rebalance','approved','[]','x')")
    asyncio.run(ex.execute(OrderIntent("i-liq", "operating", ex.account_id, "crypto", inst, Side.BUY, qty, price, price,
                                       [("trend_sma", qty)], True)))
    for _ in range(6):
        ctx.clock.advance(20)
        ctx.market_store.save_quotes(asyncio.run(ctx.markets["crypto"].data.quotes([inst])))
        asyncio.run(ex.poll())


def test_liquidation_requires_phrase_and_uses_limit_sells_of_bot_qty(demo_ctx):
    ctx = demo_ctx
    if not _op_qty(ctx):
        _ensure_position(ctx)
    held = _op_qty(ctx)
    assert held, "보유분 준비 실패"
    pv = actions.preview_liquidation(ctx, "crypto")
    assert pv.confirm_phrase == "청산 crypto" and any("시장가 전량 매도가 아니" in w for w in pv.warnings)
    with pytest.raises(PermissionError):
        asyncio.run(actions.liquidate(ctx, "crypto", "청산", "test"))
    orders = asyncio.run(actions.liquidate(ctx, "crypto", "청산 crypto", "test"))
    assert ctx.flags.halted("crypto") is not None  # 청산은 신규 매수 중지를 함께 켠다
    rows = ctx.db.query("SELECT * FROM orders WHERE purpose='liquidation'")
    assert rows
    for r in rows:
        assert r["side"] == "sell" and r["order_type"] == "limit"
        q = ctx.market_store.latest_quote(r["instrument_id"])
        assert Decimal(r["limit_price"]) <= q.bid  # 매수1호가 이하 지정가
        assert Decimal(r["qty"]) <= held[r["instrument_id"]]  # 봇 장부 수량만
    assert len(orders) >= 1
