import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from aifund.cli import _config_path
from aifund.config.settings import Settings, load_settings_file
from aifund.core.paths import mode_paths
from aifund.core.timeutil import ManualClock, UTC, to_iso
from aifund.domain.models import FxRate
from aifund.ledger.valuation import value_book
from aifund.service.context import build_context
from aifund.service.cycle import DecisionCycle
from aifund.service.paper_fx import fund_us_paper
from aifund.service.runtime import Runtime
from aifund.web.app import create_app
from helpers import inst

PROFILE =Path(__file__).resolve().parents[1] / "config" / "paper.toml"


def context(home):
    return build_context(mode_paths("offline_demo", home), config_path=PROFILE,
                         clock=ManualClock(datetime(2026, 9, 29, 14, 0, tzinfo=UTC)))


def test_paper_profile_and_live_separation(home):
    s = load_settings_file(PROFILE)
    assert s.risk.principal_cap_krw == 5000000
    assert s.enabled_markets() == ["crypto", "kr_stock", "us_stock"]
    assert sum(m.allocation_krw for m in s.markets.values()) == 5000000
    assert s.operating_setting == "C" and s.ai.provider == "gemini"
    assert "반도체" in s.ai.research_focus
    for name in ("paper.toml", "config.toml"):
        (home / "config" / name).write_text("", encoding="utf-8")
    assert _config_path("internal_paper").name == "paper.toml"
    assert _config_path("live").name == "config.toml"
    assert _config_path("broker_sandbox").name == "config.toml"
    with pytest.raises(ValueError):
        Settings.model_validate({"markets": {"crypto": {"allocation_krw": "-1"}}})


def test_fx_is_atomic_bounded_and_not_repeated(home):
    ctx = context(home)
    ex = ctx.executor_for("operating", "us_stock")
    assert fund_us_paper(ctx, "operating", ex) == 0  # no FX yet
    asyncio.run(ctx.fx.refresh())
    usd = fund_us_paper(ctx, "operating", ex)
    assert usd > 0
    assert fund_us_paper(ctx, "operating", ex) == 0
    balances = asyncio.run(ex.broker.balances())
    assert balances["USD"].free == ctx.ledger.cash("operating", "USD") == usd
    assert balances["KRW"].free == ctx.ledger.cash("operating", "KRW")
    assert ctx.ledger.cash("operating") + usd * 1400 == 5000000
    assert ctx.ledger.principal("operating") == 5000000
    restarted = build_context(ctx.paths, clock=ctx.clock, config_path=PROFILE)
    assert fund_us_paper(restarted, "operating", restarted.executor_for("operating", "us_stock")) == 0
    assert restarted.ledger.cash("operating", "USD") == usd


def test_fx_rejects_stale_and_real_broker(home):
    ctx = context(home)
    ex = ctx.executor_for("operating", "us_stock")
    ctx.fx.record(FxRate("USDKRW", D(1400), ctx.clock.now() - timedelta(days=10), ctx.clock.now(), "test"))
    assert fund_us_paper(ctx, "operating", ex) == 0
    from aifund.brokers.fake import FakeBroker
    ex.broker = FakeBroker("real-test")
    asyncio.run(ctx.fx.refresh())
    assert fund_us_paper(ctx, "operating", ex) == 0


def test_existing_paper_capital_upgrade_only_once(home):
    paths = mode_paths("offline_demo", home)
    old = build_context(paths)
    assert old.ledger.principal("operating") == 300000
    ctx = build_context(paths, config_path=PROFILE)
    assert ctx.ledger.principal("operating") == ctx.ledger.cash("operating") == 5000000
    balances = asyncio.run(ctx.executor_for("operating", "crypto").broker.balances())
    assert balances["KRW"].free == 5000000
    again = build_context(paths, config_path=PROFILE)
    assert again.ledger.principal("operating") == 5000000


def test_one_market_failure_does_not_block_quotes_or_ai(home):
    ctx = context(home)
    rt = Runtime(ctx)
    async def run():
        for market, mr in ctx.markets.items():
            await mr.collector.refresh_instruments(market, ctx.settings.markets[market].instruments)
        ctx.markets["crypto"].data.quotes = AsyncMock(side_effect=RuntimeError("unavailable"))
        await rt.poll_quotes()
        assert ctx.market_store.latest_quote("us_stock:NASD:NVDA") is not None
        assert "quotes:crypto" in rt.errors
        ctx.ai.due_research = lambda market: True
        ctx.ai.due_weekly = lambda: False
        rt.run_research = AsyncMock(side_effect=[RuntimeError("failed"), None, None])
        await rt.ai_tick()
        assert rt.run_research.await_count == 3
        assert "ai:crypto" in rt.errors
    asyncio.run(run())


def test_stock_demo_daily_data_and_us_orders(home):
    ctx = context(home)
    cycle = DecisionCycle(ctx)
    async def run():
        await ctx.fx.refresh()
        for market in ("kr_stock", "us_stock"):
            snap = await ctx.markets[market].collector.build(market, ctx.settings.markets[market], ctx.settings.risk)
            assert all(it.instrument.qty_step == 1 for it in snap.items.values())
            assert all(not any("완성봉" in issue for issue in it.issues) for it in snap.items.values())
        ex = ctx.executor_for("operating", "us_stock")
        ctx.startup_reconciled[ex.account_id] = True
        await cycle.run("us_stock")
        # The baseline must actually place USD orders through the central executor.
        assert ctx.db.scalar("SELECT COUNT(*) FROM orders WHERE market='us_stock' AND book_id='baseline_bh'") > 0
        ctx.clock.advance(30)
        await Runtime(ctx).poll_quotes()
        for executor in ctx.all_executors():
            await executor.poll()
        assert ctx.ledger.positions("baseline_bh")
        assert ctx.ledger.verify("baseline_bh") == []
        from aifund.web.views import capital
        from aifund.evaluation.metrics import book_metrics
        expected_fees = sum((D(r[0]) for r in ctx.db.query("SELECT fee FROM fills f JOIN orders o ON o.order_id=f.order_id WHERE o.book_id='baseline_bh'")), D(0)) * 1400
        assert capital(ctx, "baseline_bh")["fees"] == expected_fees
        assert book_metrics(ctx, "baseline_bh").fees == expected_fees
    asyncio.run(run())
    client = TestClient(create_app(ctx, None), base_url="http://127.0.0.1:8765")
    page = client.get("/")
    assert page.status_code == 200 and "최소 약" in page.text


def test_backtest_and_order_caps_follow_market(home, capsys):
    from aifund.cli import main
    from aifund.evaluation.backtest import side_fee_rate
    from aifund.service.cycle import order_cap_krw

    s = load_settings_file(PROFILE)
    p = s.execution.paper
    assert side_fee_rate(p, "crypto") == D("0.0005")
    assert side_fee_rate(p, "kr_stock") == D("0.00015") + D("0.0010")  # 매도세 절반씩
    assert side_fee_rate(p, "us_stock") == D("0.0025")
    # 달러 종목은 환율 여유(3%)만큼 1회 주문 요청 한도가 낮다(사이클·대시보드 공통 계산)
    assert order_cap_krw(s.risk, inst("NVDA", "us_stock", "USD", "us")) == s.risk.max_order_notional_krw * D("0.98") / D("1.03")
    assert order_cap_krw(s.risk, inst("005930", "kr_stock", "KRW", "krx")) == s.risk.max_order_notional_krw * D("0.98")

    (home / "config" / "paper.toml").write_text(PROFILE.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["--mode", "offline_demo", "backtest", "--market", "us_stock", "--days", "150", "--fetch"]) == 0
    out = capsys.readouterr().out
    assert "USD, 환율 1400" in out and "편도 비용률 0.250%" in out


def test_market_strategy_pnl_is_separate(home):
    ctx = context(home)
    async def prepare():
        for market in ("crypto", "kr_stock"):
            mr = ctx.markets[market]
            insts = await mr.collector.refresh_instruments(market, ctx.settings.markets[market].instruments)
            ctx.market_store.save_quotes(await mr.data.quotes(insts))
    asyncio.run(prepare())
    with ctx.db.tx() as c:
        for iid, realized in (("crypto:KRW-BTC", D(123)), ("kr_stock:005930", D(456))):
            ctx.ledger._upsert_pos(c, "operating", "trend_sma", iid, D(0), D(0), realized, D(0), None, to_iso(ctx.clock.now()))
    v = value_book(ctx.db, ctx.ledger, "operating", ctx.price, ctx.market_store.instrument, ctx.fx, ctx.settings.risk)
    assert v.by_strategy["trend_sma"]["realized"] == 579
    assert v.by_market_strategy["crypto"]["trend_sma"]["realized"] == 123
    assert v.by_market_strategy["kr_stock"]["trend_sma"]["realized"] == 456
