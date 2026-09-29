"""업비트 어댑터 계약 테스트(가짜 HTTP 전송, 실제 요청 없음). 공식 문서 형식을 따르는지 확인한다."""

import asyncio
import hashlib
import json
from decimal import Decimal
from urllib.parse import unquote

import httpx
import jwt

from aifund.brokers.ratelimit import RateLimiter, parse_upbit_remaining
from aifund.brokers.upbit import UpbitBroker, UpbitHttp, UpbitMarketData, build_query
from aifund.core.secrets import UpbitCreds
from aifund.domain.models import OrderRequest, OrderStatus, Side
from helpers import inst

CREDS = UpbitCreds("access-key-test", "s" * 64)


def _broker(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    http = UpbitHttp(creds=CREDS, scope="t", client=client, limiter=RateLimiter())
    return UpbitBroker("upbit-main", CREDS, live_money=True, http=http)


def test_jwt_query_hash_matches_docs():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["Authorization"]
        return httpx.Response(200, json=[], headers={"Remaining-Req": "group=default; min=1800; sec=29"})

    b = _broker(handler)
    asyncio.run(b.open_orders([inst("KRW-BTC")]))
    token = seen["auth"].split(" ", 1)[1]
    payload = jwt.decode(token, CREDS.secret_key, algorithms=["HS512"])
    qs = unquote(seen["url"].split("?", 1)[1])
    assert "states[]=wait&states[]=watch" in qs
    assert payload["query_hash"] == hashlib.sha512(qs.encode()).hexdigest()
    assert payload["query_hash_alg"] == "SHA512" and payload["access_key"] == CREDS.access_key and payload["nonce"]


def test_order_body_and_identifier():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen["body"] = body
        token = req.headers["Authorization"].split(" ", 1)[1]
        p = jwt.decode(token, CREDS.secret_key, algorithms=["HS512"])
        _, raw = build_query(body)
        assert p["query_hash"] == hashlib.sha512(raw.encode()).hexdigest()
        return httpx.Response(201, json={"uuid": "u-1", "state": "wait"})

    b = _broker(handler)
    r = asyncio.run(b.submit_order(OrderRequest("afabc", inst("KRW-BTC"), Side.BUY, Decimal("0.001"), Decimal("114000000"))))
    assert r.outcome == "accepted" and r.broker_order_id == "u-1"
    assert seen["body"] == {"market": "KRW-BTC", "side": "bid", "volume": "0.001", "price": "114000000", "ord_type": "limit",
                            "identifier": "afabc"}


def _submit_with(resp_or_exc):
    def handler(req):
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    return asyncio.run(_broker(handler).submit_order(OrderRequest("afx", inst("KRW-BTC"), Side.BUY, Decimal("0.001"), Decimal("114000000"))))


def test_submit_outcome_classification():
    err = lambda code, name: httpx.Response(code, json={"error": {"name": name, "message": "m"}})  # noqa: E731
    assert _submit_with(err(400, "insufficient_funds_bid")).outcome == "rejected"
    assert _submit_with(err(401, "jwt_verification")).outcome == "rejected"
    assert _submit_with(err(429, "too_many")).outcome == "rejected"
    assert _submit_with(err(400, "duplicated_identifier")).outcome == "unknown"  # 이미 있음 → 조회
    assert _submit_with(httpx.Response(500, text="oops")).outcome == "unknown"  # 서버 오류: 단정 금지
    assert _submit_with(httpx.ReadTimeout("t")).outcome == "unknown"  # 전송 후 응답 유실
    assert _submit_with(httpx.ConnectError("c")).outcome == "rejected"  # 연결 전 실패 = 미전송


def test_get_order_maps_trades_and_not_found():
    order = {"uuid": "u1", "identifier": "af1", "market": "KRW-BTC", "side": "bid", "ord_type": "limit", "price": "100",
             "state": "wait", "volume": "2", "executed_volume": "1", "paid_fee": "0.05",
             "trades": [{"uuid": "t1", "price": "100", "volume": "1", "funds": "100", "created_at": "2025-08-09T16:44:00+09:00"}]}

    def handler(req):
        if "identifier=missing" in str(req.url):
            return httpx.Response(404, json={"error": {"name": "order_not_found", "message": "x"}})
        return httpx.Response(200, json=order)

    b = _broker(handler)
    st = asyncio.run(b.get_order(broker_order_id=None, client_order_id="af1"))
    assert st.status == OrderStatus.PARTIALLY_FILLED and st.filled_qty == 1 and st.fee_total == Decimal("0.05")
    assert st.trades[0].trade_id == "t1"
    assert asyncio.run(b.get_order(broker_order_id=None, client_order_id="missing")) is None


def test_remaining_req_header():
    assert parse_upbit_remaining("group=default; min=1800; sec=29") == ("default", 29)
    assert parse_upbit_remaining(None) == (None, None)


def test_candles_sorted_with_close_time():
    data = [{"market": "KRW-BTC", "candle_date_time_utc": "2025-07-01T13:00:00", "opening_price": 2, "high_price": 3,
             "low_price": 1, "trade_price": 2, "candle_acc_trade_volume": 1, "candle_acc_trade_price": 2},
            {"market": "KRW-BTC", "candle_date_time_utc": "2025-07-01T12:00:00", "opening_price": 1, "high_price": 2,
             "low_price": 1, "trade_price": 2, "candle_acc_trade_volume": 1, "candle_acc_trade_price": 2}]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
    md = UpbitMarketData(UpbitHttp(client=client, limiter=RateLimiter()))
    cs = asyncio.run(md.candles(inst("KRW-BTC"), "60m", 2))
    assert cs[0].open_time < cs[1].open_time
    assert (cs[0].close_time - cs[0].open_time).total_seconds() == 3600
