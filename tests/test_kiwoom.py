import asyncio
from datetime import datetime

import httpx
import pytest

from aifund.brokers.base import BrokerError
from aifund.brokers.kiwoom import KiwoomReadClient, KiwoomMarketData, number
from aifund.core.secrets import KiwoomCreds, load_mode_secrets
from aifund.core.timeutil import ManualClock, UTC


class NoWait:
    def configure(self, *a, **k): pass
    async def acquire(self, *a): pass
    def block(self, *a): pass


def client(http):
    return KiwoomReadClient(KiwoomCreds("key", "secret", "mock"), client=http, limiter=NoWait(),
                           clock=ManualClock(datetime(2026, 9, 29, 0, tzinfo=UTC)))


def test_auth_refresh_and_order_rejected():
    counts = {"auth": 0, "quote": 0}
    def handler(r):
        assert r.url.host == "mockapi.kiwoom.com"
        if r.url.path == "/oauth2/token":
            counts["auth"] += 1
            return httpx.Response(200, json={"return_code": 0, "token": "token-value", "expires_dt": "20260930090000"})
        counts["quote"] += 1
        assert r.headers["authorization"] == "Bearer token-value"
        if counts["quote"] == 1:
            return httpx.Response(200, json={"return_code": 3, "return_msg": "[8005:expired]"})
        return httpx.Response(200, json={"return_code": "0000", "cur_prc": "-70000", "buy_1bid": "-69900", "sel_1bid": "+70000"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            c = client(h)
            with pytest.raises(BrokerError, match="조회 전용"):
                await c.call("kt10000", {})
            assert counts["auth"] == 0
            d = KiwoomMarketData(c)
            inst = await d.instruments("kr_stock", ["005930"])
            q = (await d.quotes(inst))[0]
            assert q.last == 70000 and q.bid == 69900 and q.ts_exchange is None
            assert counts == {"auth": 2, "quote": 2}
    asyncio.run(run())


def test_pagination_and_signed_cash():
    def handler(r):
        if r.url.path == "/oauth2/token":
            return httpx.Response(200, json={"return_code": 0, "token": "token", "expires_dt": "20260930090000"})
        if r.headers["api-id"] == "kt00001":
            return httpx.Response(200, json={"return_code": 0, "entr": "-100", "ord_alow_amt": "0"})
        more = not r.headers["next-key"]
        return httpx.Response(200, headers={"cont-yn": "Y" if more else "N", "next-key": "next"}, json={
            "return_code": 0, "acnt_evlt_remn_indv_tot": [{"stk_cd": "A005930" if more else "A000660",
            "stk_nm": "test", "rmnd_qty": "1", "pur_pric": "70000"}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            result = await client(h).account_summary()
            assert result["cash_krw"] == -100 and len(result["holdings"]) == 2
    asyncio.run(run())


def test_bad_pagination_fails_closed():
    async def run():
        c = client(None)
        async def fake(*a): return {"rows": []}, {"cont-yn": "Y", "next-key": "same"}
        c.call = fake
        try:
            with pytest.raises(BrokerError, match="연속조회"):
                await c.rows("kt00018", {}, "rows")
        finally:
            await c.close()
    asyncio.run(run())


def test_daily_candles_and_calendar():
    async def run():
        c = client(None)
        async def rows(*a, **k):
            return [{"dt": date, "open_pric": "-70000", "high_pric": "+71000", "low_pric": "69000",
                     "cur_prc": "-70500", "trde_qty": "100"} for date in ["20260929", "20260928"]]
        c.rows = rows
        try:
            d = KiwoomMarketData(c)
            inst = (await d.instruments("kr_stock", ["005930"]))[0]
            candles = await d.candles(inst, "1d", 2)
            assert len(candles) == 2 and candles[0].open_time < candles[1].open_time
            assert candles[-1].close_time.hour == 6 and candles[-1].close == 70500
            with pytest.raises(BrokerError): await d.candles(inst, "60m", 2)
        finally:
            await c.close()
    asyncio.run(run())


def test_mode_isolation_and_context_selection(home, monkeypatch):
    from aifund.core.paths import mode_paths
    from aifund.service.context import build_context, _setup_markets
    monkeypatch.setenv("KIWOOM_DATA_APP_KEY", "test-key")
    monkeypatch.setenv("KIWOOM_DATA_APP_SECRET", "test-secret")
    monkeypatch.setenv("KIWOOM_DATA_ENV", "mock")
    assert load_mode_secrets("offline_demo").kiwoom_data is None
    ctx = build_context(mode_paths("internal_paper", home).ensure())
    ctx.settings.markets["crypto"].enabled = False
    ctx.settings.markets["kr_stock"].enabled = True
    ctx.settings.markets["kr_stock"].data_provider = "kiwoom"
    _setup_markets(ctx, None)
    mr = ctx.markets["kr_stock"]
    assert isinstance(mr.data, KiwoomMarketData)
    assert not mr.operating_broker.is_live_money
    asyncio.run(mr.data.close())
    with pytest.raises(BrokerError): number("NaN")


def test_kiwoom_env_value_checked_only_with_keys(monkeypatch):
    monkeypatch.setenv("KIWOOM_DATA_ENV", "prod")
    assert load_mode_secrets("internal_paper").kiwoom_data is None  # 키움을 안 쓰면 다른 시장까지 막지 않음
    monkeypatch.setenv("KIWOOM_DATA_APP_KEY", "test-key")
    monkeypatch.setenv("KIWOOM_DATA_APP_SECRET", "test-secret")
    with pytest.raises(ValueError, match="KIWOOM_DATA_ENV"):
        load_mode_secrets("internal_paper")
    monkeypatch.setenv("KIWOOM_DATA_ENV", "")  # 비워 두면 mock
    assert load_mode_secrets("internal_paper").kiwoom_data.env == "mock"
    monkeypatch.setenv("KIWOOM_DATA_ENV", " Real ")
    assert load_mode_secrets("internal_paper").kiwoom_data.env == "real"


def test_us_quotes_daily_and_order_isolation():
    import json
    from decimal import Decimal
    from aifund.brokers.kiwoom import KiwoomUSMarketData

    def handler(r):
        if r.url.path == "/oauth2/token":
            return httpx.Response(200, json={"return_code": 0, "token": "t", "expires_dt": "20260930090000"})
        body = json.loads(r.content)
        assert body["stex_tp"] == "ND" and body["stk_cd"] == "NVDA"
        if r.headers["api-id"] == "usa20101":
            return httpx.Response(200, json={"return_code": 0, "stex_tp": "ND", "stk_cd": "NVDA",
                "cur_prc": "+230.7500", "buy_1bid": "230.7300", "sel_1bid": "230.7600",
                "buy_1bid_req": "10", "sel_1bid_req": "20"})
        assert r.headers["api-id"] == "usa06012" and body["exrt_appl_tp"] == "0"
        return httpx.Response(200, json={"return_code": 0, "result_list": [
            {"dt": "20260928", "open_pric": "230.0000", "high_pric": "231.0000", "low_pric": "229.0000",
             "cur_prc": "230.5000", "acc_trde_qty": "100"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            c = client(h)
            data = KiwoomUSMarketData(c)
            inst = (await data.instruments("us_stock", ["NASD:NVDA"]))[0]
            assert inst.quote_ccy == "USD" and inst.qty_step == 1
            q = (await data.quotes([inst]))[0]
            assert q.ask == Decimal("230.76") and q.bid_size == 10
            bars = await data.candles(inst, "1d", 2)
            assert bars[0].close_time.hour == 20 and bars[0].close == Decimal("230.5")
            with pytest.raises(BrokerError):
                await c.call("ust10000", {})
            with pytest.raises(BrokerError):
                await data.instruments("us_stock", ["INVALID:NVDA"])
            with pytest.raises(BrokerError):
                await data.candles(inst, "60m", 2)
    asyncio.run(run())


def test_paper_profile_uses_kiwoom_for_both_markets(home, monkeypatch):
    from pathlib import Path
    from aifund.core.paths import mode_paths
    from aifund.service.context import build_context
    from aifund.brokers.kiwoom import KiwoomUSMarketData
    monkeypatch.setenv("KIWOOM_DATA_APP_KEY", "test-key")
    monkeypatch.setenv("KIWOOM_DATA_APP_SECRET", "test-secret")
    profile = Path(__file__).resolve().parents[1] / "config" / "paper.toml"
    ctx = build_context(mode_paths("internal_paper", home), config_path=profile)
    assert isinstance(ctx.markets["us_stock"].data, KiwoomUSMarketData)
    assert ctx.markets["us_stock"].data.c is ctx.markets["kr_stock"].data.c
    assert ctx.markets["us_stock"].operating_broker.name == "paper"
    assert not ctx.markets["us_stock"].operating_broker.is_live_money
    async def close():
        for mr in ctx.markets.values():
            if mr.data: await mr.data.close()
    asyncio.run(close())
