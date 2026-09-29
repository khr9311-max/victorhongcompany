"""검증 5: 프로세스 재시작 후 잔고·미체결을 대사하고 오래된 신호를 폐기하는가."""

import asyncio
from datetime import datetime, timedelta

from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock, to_iso
from aifund.service.context import build_context
from aifund.service.cycle import DecisionCycle
from aifund.service.runtime import Runtime


def test_restart_recovers_and_reconciles(home):
    clock = ManualClock(datetime.now(UTC) - timedelta(hours=3))
    paths = mode_paths("offline_demo", home)
    ctx = build_context(paths.ensure(), clock=clock)
    rt = Runtime(ctx)
    asyncio.run(rt.startup())
    ex = ctx.markets["crypto"].operating_executor
    inst = ctx.market_store.instrument("crypto:KRW-BTC")
    q = ctx.market_store.save_quotes(asyncio.run(ctx.markets["crypto"].data.quotes([inst])))[0]
    from aifund.execution.executor import OrderIntent
    from aifund.domain.models import Side
    from aifund.core.money import D

    ctx.db.execute("INSERT INTO intents(intent_id, book_id, instrument_id, side, qty, ref_price, notional_krw, risk_increasing, purpose, "
                   "status, allocations_json, created_at) VALUES ('i-crash','operating','crypto:KRW-BTC','buy','1',?,?,1,'rebalance',"
                   "'approved','[]',?)", (str(q.ask), str(q.ask), to_iso(clock.now())))
    oid = ex.reserve_and_create(OrderIntent("i-crash", "operating", ex.account_id, "crypto", inst, Side.BUY, D(1), q.ask, q.ask,
                                            [("trend_sma", D(1))], True))
    # 전송 시도 기록 직후 crash 가정
    ctx.db.execute("UPDATE orders SET submit_attempted_at=? WHERE order_id=?", (to_iso(clock.now()), oid))
    ctx.db.close()

    ctx2 = build_context(paths, clock=clock)
    rt2 = Runtime(ctx2)
    assert not ctx2.startup_reconciled  # 시작 직후에는 대사 전 → 신규 주문 금지
    asyncio.run(rt2.startup())
    row = ctx2.db.query_one("SELECT status FROM orders WHERE order_id=?", (oid,))
    # 모의 거래소에는 주문이 없으므로 조회 반복 후 미접수 확정 전까지는 unknown
    assert row["status"] in ("unknown", "rejected")
    assert ctx2.db.scalar("SELECT COUNT(*) FROM incidents WHERE category='crash_recovery'") == 1
    assert ctx2.startup_reconciled.get(ex.account_id) is True
    assert ctx2.db.scalar("SELECT COUNT(*) FROM reconciliations") >= 2


def test_missed_candle_signal_is_skipped(home):
    clock = ManualClock(datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=5))
    ctx = build_context(mode_paths("offline_demo", home).ensure(), clock=clock)
    cycle = DecisionCycle(ctx)
    asyncio.run(ctx.markets["crypto"].collector.refresh_instruments("crypto", ctx.settings.markets["crypto"].instruments))
    clock.advance(minutes=40)  # 봉 마감 후 40분(> max_signal_age 15분)
    res = asyncio.run(cycle.run("crypto"))
    assert res.status == "skipped_stale"
    assert ctx.db.scalar("SELECT COUNT(*) FROM orders") == 0
    rt = Runtime(ctx)
    key, run_at, why = rt.decision_target("crypto", clock.now())
    assert key is None and "다음 봉" in why  # 놓친 봉은 몰아서 실행하지 않고 다음 봉을 기다린다
