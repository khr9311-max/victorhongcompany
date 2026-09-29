"""LIVE 활성화 전체 흐름(가짜 실계좌, 실제 네트워크 없음)."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from aifund.brokers.fake import FakeBroker
from aifund.control.readiness import enable_live, live_readiness
from aifund.control.selftest import run_selftest
from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock, floor_to_interval
from aifund.data.replay import ReplayMarketData
from aifund.domain.models import Side
from aifund.execution.executor import OrderIntent
from aifund.service.context import build_context
from helpers import candles_series


def test_enable_live_allocates_min_and_keeps_existing_holdings(home):
    now = floor_to_interval(datetime.now(UTC), 60) + timedelta(seconds=30)
    clock = ManualClock(now)
    start = now - timedelta(hours=120)
    replay = ReplayMarketData({f"crypto:{s}": candles_series(f"crypto:{s}", [10000 + i for i in range(121)],
                                                             floor_to_interval(start, 60))
                               for s in ("KRW-BTC", "KRW-ETH", "KRW-XRP")}, clock, top_size=Decimal(1000))
    fb = FakeBroker("upbit-main", live_money=True, cash=Decimal("200000"))  # 서브포켓에 20만원만 있음
    fb.holdings = {"crypto:KRW-BTC": Decimal("3")}  # 활성화 전부터 있던 보유분
    ctx = build_context(mode_paths("live", home).ensure(), clock=clock, replay=replay,
                        live_broker_factory=lambda market, ms: fb)
    assert ctx.ledger.principal("operating") == 0  # live는 원금 0에서 시작
    asyncio.run(run_selftest(ctx.db, ctx.code_version))

    items = asyncio.run(live_readiness(ctx, "crypto", ack_no_withdraw=False))
    assert not next(i for i in items if "출금 권한" in i.name).ok  # 사용자 확인 없으면 실패

    ok, _, msg = asyncio.run(enable_live(ctx, "crypto", "LIVE crypto wrong", "test", ack_no_withdraw=True))
    assert not ok and "확인 문구" in msg
    ok, items, msg = asyncio.run(enable_live(ctx, "crypto", "LIVE crypto upbit-main", "test", ack_no_withdraw=True))
    assert ok, (msg, [(i.name, i.detail) for i in items if not i.ok])
    # 배정 = min(설정 30만원, 실제 주문가능 20만원)
    assert ctx.ledger.principal("operating") == Decimal("200000")
    base = ctx.db.query_one("SELECT qty FROM account_baselines WHERE account_id='upbit-main' AND asset='crypto:KRW-BTC'")
    assert Decimal(base["qty"]) == 3  # 기존 보유분은 봇 자산이 아님
    assert ctx.markets["crypto"].operating_executor is not None

    # 활성화 후에는 가드를 통과해 (가짜) 실계좌 어댑터로 주문이 간다
    ex = ctx.markets["crypto"].operating_executor
    inst = ctx.market_store.instrument("crypto:KRW-ETH")
    q = ctx.market_store.latest_quote(inst.instrument_id)
    ctx.db.execute("INSERT INTO intents(intent_id, book_id, instrument_id, side, qty, ref_price, notional_krw, risk_increasing, purpose, "
                   "status, allocations_json, created_at) VALUES ('i-live','operating','crypto:KRW-ETH','buy','2',?,?,1,'rebalance',"
                   "'approved','[]','x')", (str(q.ask), str(q.ask)))
    oid, st = asyncio.run(ex.execute(OrderIntent("i-live", "operating", ex.account_id, "crypto", inst, Side.BUY, Decimal(2), q.ask,
                                                 q.ask, [("trend_sma", Decimal(2))], True)))
    assert st == "submitted" and fb.submit_calls == 1
    # 봇은 기존 보유 BTC를 매도할 수 없다(봇 장부 수량 0)
    assert ex.sellable_qty("operating", "crypto:KRW-BTC") == 0
    # 설정 범위를 바꾸면 LIVE는 다시 확인이 필요해진다
    ctx.settings.markets["crypto"].instruments = ["KRW-BTC", "KRW-ETH"]
    okk, why = ctx.activations.authorized("live", "crypto", "upbit-main", "crypto:KRW-ETH", ctx.settings)
    assert not okk and "재확인" in why
