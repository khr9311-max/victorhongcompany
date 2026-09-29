"""검증 6: 원화·외화·코인·예약금 합산 예산, 수수료, 최소 주문, 정밀도 검사."""

from datetime import timedelta
from decimal import Decimal

import pytest

from aifund.core.money import D, ceil_step, dstr, floor_step
from aifund.data.fx import FxService
from aifund.domain.models import FxRate, Quote, Side
from aifund.execution.executor import ReservationDenied
from aifund.ledger.valuation import value_book
from aifund.markets.rules import krx_tick, upbit_krw_tick, us_tick
from aifund.service.cycle import limit_price
from helpers import inst, make_env, trade


def test_tick_tables():
    assert upbit_krw_tick(D("114163000")) == 1000
    assert upbit_krw_tick(D("3684000")) == 1000
    assert upbit_krw_tick(D("750000")) == 500
    assert upbit_krw_tick(D("2047")) == 1
    assert upbit_krw_tick(D("15")) == D("0.1")  # 문서 예시: 15원 → 14.9, 15.0, 15.1 …
    assert upbit_krw_tick(D("5")) == D("0.01")
    assert krx_tick(D("1999")) == 1 and krx_tick(D("4995")) == 5 and krx_tick(D("70000")) == 100 and krx_tick(D("600000")) == 1000
    assert us_tick(D("187.25")) == D("0.01") and us_tick(D("0.52")) == D("0.0001")
    # 거래소 API 호가 단위가 더 크면 더 큰 단위를 쓴다
    i = inst()
    i2 = type(i)(**{**i.__dict__, "fixed_tick": D("5")})
    assert i2.tick_for(D("2047")) == 5


def test_limit_price_rounding_marketable_and_on_tick():
    i = inst()
    q = Quote(i.instrument_id, D("114050000"), D("114060000"), D(1), D(1), None, None, None, __import__("helpers").T0, "t")
    buy = limit_price(i, q, Side.BUY, D("10"))
    sell = limit_price(i, q, Side.SELL, D("10"))
    assert buy >= q.ask and buy % 1000 == 0 and buy <= q.ask * D("1.001")
    assert sell <= q.bid and sell % 1000 == 0 and sell >= q.bid * D("0.999")


def test_decimal_helpers():
    assert floor_step(D("0.123456789"), D("0.00000001")) == D("0.12345678")
    assert ceil_step(D("10.01"), D("1")) == D("11")
    assert dstr(D("1E+3")) == "1000"
    assert D(0.1) + D(0.2) == D("0.3")


def test_min_order_and_step_enforced_by_risk_and_paper(tmp_path):
    from aifund.risk.engine import RiskInputs, evaluate

    env = make_env(tmp_path)
    q = env.quote()
    base = dict(mode="internal_paper", market="crypto", book_kind="operating", account_id="acct", instrument=env.inst,
                side=Side.BUY, risk_increasing=True, quote=q, now=env.clock.now(), notional_krw=D(4000))
    d = evaluate(RiskInputs(qty=D("0.4"), limit_price=D(10000), **base), env.settings.risk)  # 4,000원 < 5,000원
    assert not d.approved and any("최소 주문 금액" in r for r in d.reasons)
    d = evaluate(RiskInputs(qty=D("1.000000001"), limit_price=D(10000), **{**base, "notional_krw": D(10000)}), env.settings.risk)
    assert any("수량 단위" in r for r in d.reasons)
    d = evaluate(RiskInputs(qty=D("1"), limit_price=D("10000.5"), **{**base, "notional_krw": D(10000)}), env.settings.risk)
    assert any("호가 단위" in r for r in d.reasons)


def test_mixed_currency_budget_and_reservations(tmp_path):
    env = make_env(tmp_path)
    env.fx.record(FxRate("USDKRW", D("1400"), env.clock.now() - timedelta(hours=1), env.clock.now(), "test"))
    us = inst("AAPL", market="us_stock", ccy="USD", tick_policy="us", step=D(1), min_notional=D("0.01"))
    env.store.save_instruments([us])
    env.quote(bid="199.99", ask="200.00", iid=us.instrument_id)
    # 장부에 USD 현금(가상 배정)과 코인 보유를 기록
    with env.db.tx() as c:
        c.execute("INSERT INTO ledger_entries(book_id, ts, kind, strategy_id, asset, delta) VALUES ('b', ?, 'adjustment', '_book', 'KRW', '-140000')",
                  (env.clock.now().isoformat(),))
        c.execute("INSERT INTO ledger_entries(book_id, ts, kind, strategy_id, asset, delta) VALUES ('b', ?, 'adjustment', '_book', 'USD', '100')",
                  (env.clock.now().isoformat(),))
    env.broker.default_script = [[trade("c1", "5", "10000", "25")]]
    import asyncio

    asyncio.run(env.executor.execute(env.intent("5")))
    asyncio.run(env.executor.poll())
    env.broker.default_script = None
    oid, st = asyncio.run(env.executor.execute(env.intent("3")))  # 미체결 매수 예약 3만원+α
    v = value_book(env.db, env.ledger, "b", lambda iid: env.store.latest_quote(iid).mid, env.store.instrument, env.fx, env.settings.risk)
    # 현금(KRW) = 300,000 - 140,000 - 50,000 - 25(수수료)
    assert v.cash["KRW"] == D("109975")
    assert v.cash["USD"] == D("100")
    assert v.cash_krw == D("109975") + D("140000")  # USD 100 × 1400
    assert v.positions_krw == D(5) * D("9995")
    assert v.reserved_buy_krw > D(30000)  # 수수료 여유 포함 예약
    assert v.exposure_krw == v.positions_krw + v.reserved_buy_krw
    assert v.equity_krw == v.cash_krw + v.positions_krw


def test_stale_fx_blocks_usd_valuation(tmp_path):
    env = make_env(tmp_path)
    fx = FxService(env.db, clock=env.clock, provider="none")
    fx.record(FxRate("USDKRW", D("1400"), env.clock.now() - timedelta(hours=200), env.clock.now(), "test"))
    v, why = fx.to_krw(D(100), "USD", 96)
    assert v is None and "지남" in why
    v, _ = fx.to_krw(D(100), "KRW", 96)
    assert v == 100


def test_gross_exposure_cap_counts_reserved_orders(tmp_path):
    env = make_env(tmp_path)
    env.settings.risk.gross_exposure_cap_krw = D(100000)
    env.settings.risk.max_order_notional_krw = D(100000)
    import asyncio

    a, _ = asyncio.run(env.executor.execute(env.intent("6")))  # 약 6만원 예약(미체결)
    assert a is not None
    with pytest.raises(ReservationDenied, match="총노출 한도"):
        env.executor.reserve_and_create(env.intent("5"))  # 예약 6만 + 5만 > 10만


def test_fee_is_deducted_in_ledger(tmp_path):
    env = make_env(tmp_path)
    env.broker.default_script = [[trade("f1", "5", "10000", "25")]]
    import asyncio

    asyncio.run(env.executor.execute(env.intent("5")))
    asyncio.run(env.executor.poll())
    p = env.ledger.position("b", "trend_sma", env.inst.instrument_id)
    assert p.cost_basis == D(50025) and p.fees == D(25)
    assert env.ledger.cash("b") == D(300000) - D(50025)
    assert Decimal(env.db.scalar("SELECT fee FROM fills")) == D(25)
