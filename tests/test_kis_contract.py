"""KIS 어댑터 계약 테스트(가짜 HTTP). 실제 계좌 연결 확인이 아니다."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import httpx

from aifund.brokers.base import OrderLookupHint
from aifund.brokers.kis import TR, UNVERIFIED_TR, KisBroker, KisClient
from aifund.brokers.ratelimit import RateLimiter
from aifund.core.secrets import KisCreds
from aifund.core.timeutil import KST, ManualClock
from aifund.domain.models import OrderRequest, OrderStatus, Side
from helpers import T0, inst

KR = inst("005930", market="kr_stock", tick_policy="krx", step=Decimal(1), min_notional=Decimal(1))


def _client(handler, env="real", tmp=None):
    creds = KisCreds("appkey-123456", "appsecret-xyz", "12345678", "01", env)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clock = ManualClock(T0)
    return KisClient(creds, token_cache_dir=tmp, client=http, limiter=RateLimiter(), clock=clock), clock


def _token_resp():
    return httpx.Response(200, json={"access_token": "tok-1", "access_token_token_expired": "2026-03-03 10:00:00", "expires_in": 86400})


def test_token_cached_and_headers(tmp_path):
    calls = {"token": 0, "tr": []}

    def handler(req):
        if req.url.path == "/oauth2/tokenP":
            calls["token"] += 1
            return _token_resp()
        calls["tr"].append(req.headers["tr_id"])
        assert req.headers["authorization"] == "Bearer tok-1" and req.headers["custtype"] == "P"
        return httpx.Response(200, json={"rt_cd": "0", "output1": [], "output2": [{"dnca_tot_amt": "100000"}]})

    c, _ = _client(handler, tmp=tmp_path)
    b = KisBroker("kis-main", "kr_stock", c, live_money=True)
    asyncio.run(b.balances())
    asyncio.run(b.balances())
    assert calls["token"] == 1 and calls["tr"] == ["TTTC8434R", "TTTC8434R"]
    # 재시작 후에도 파일 캐시 재사용(토큰 재발급 빈도 제한 대응)
    c2, _ = _client(handler, tmp=tmp_path)
    asyncio.run(KisBroker("kis-main", "kr_stock", c2, live_money=True).balances())
    assert calls["token"] == 1


def test_demo_tr_ids_and_unverified_flag():
    assert TR[("dom_buy", "demo")] == "VTTC0012U" and TR[("dom_sell", "real")] == "TTTC0011U"
    assert TR[("us_sell", "real")] == "TTTT1006U"
    assert ("us_sell", "demo") in UNVERIFIED_TR


def test_submit_parses_odno_and_rejects_fractional(tmp_path):
    def handler(req):
        if req.url.path == "/oauth2/tokenP":
            return _token_resp()
        body = json.loads(req.content)
        assert body["ORD_DVSN"] == "00" and body["EXCG_ID_DVSN_CD"] == "KRX" and req.headers["tr_id"] == "VTTC0012U"
        return httpx.Response(200, json={"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000117057", "ORD_TMD": "121052"}})

    c, _ = _client(handler, env="demo", tmp=tmp_path)
    b = KisBroker("kis-demo", "kr_stock", c, live_money=False)
    r = asyncio.run(b.submit_order(OrderRequest("afx", KR, Side.BUY, Decimal(1), Decimal(70000))))
    assert r.outcome == "accepted" and r.broker_order_id == "0000117057"
    r2 = asyncio.run(b.submit_order(OrderRequest("afy", KR, Side.BUY, Decimal("0.5"), Decimal(70000))))
    assert r2.outcome == "rejected"


def test_lost_response_matching_requires_unique_candidate(tmp_path):
    rows = [
        {"ord_dt": "20260302", "odno": "0000000011", "orgn_odno": "", "sll_buy_dvsn_cd": "02", "pdno": "005930", "ord_qty": "1",
         "ord_unpr": "70000", "ord_tmd": "100005", "tot_ccld_qty": "1", "tot_ccld_amt": "70000", "rmn_qty": "0", "rjct_qty": "0",
         "cncl_yn": "N", "cnc_cfrm_qty": "0", "ord_gno_brno": "91252"},
    ]

    def handler(req):
        if req.url.path == "/oauth2/tokenP":
            return _token_resp()
        return httpx.Response(200, json={"rt_cd": "0", "output1": rows, "output2": {}})

    c, clock = _client(handler, tmp=tmp_path)
    b = KisBroker("kis-main", "kr_stock", c, live_money=True, clock=clock)
    submitted = T0.astimezone(KST).replace(hour=10, minute=0, second=0)
    hint = OrderLookupHint(KR, "buy", Decimal(1), Decimal(70000), submitted)
    st = asyncio.run(b.get_order(broker_order_id=None, client_order_id="af1", hint=hint))
    assert st is not None and st.broker_order_id == "0000000011" and st.status == OrderStatus.FILLED
    # 같은 조건 주문이 두 건이면 자동 연결하지 않는다
    rows.append({**rows[0], "odno": "0000000012", "ord_tmd": "100007"})
    try:
        asyncio.run(b.get_order(broker_order_id=None, client_order_id="af1", hint=hint))
        raise AssertionError("모호한 후보는 오류여야 함")
    except Exception as exc:
        assert "후보" in str(exc)
    # 이미 다른 주문에 연결된 번호는 제외
    hint2 = OrderLookupHint(KR, "buy", Decimal(1), Decimal(70000), submitted, frozenset({"0000000012"}))
    assert asyncio.run(b.get_order(broker_order_id=None, client_order_id="af1", hint=hint2)).broker_order_id == "0000000011"
    _ = timedelta


def test_token_expired_code_triggers_single_refresh(tmp_path):
    state = {"n": 0, "tokens": 0}

    def handler(req):
        if req.url.path == "/oauth2/tokenP":
            state["tokens"] += 1
            return _token_resp()
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(500, json={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."})
        return httpx.Response(200, json={"rt_cd": "0", "output1": [], "output2": [{"dnca_tot_amt": "1"}]})

    c, clock = _client(handler, tmp=tmp_path)
    b = KisBroker("kis-main", "kr_stock", c, live_money=True, clock=clock)
    asyncio.run(c.token())
    clock.advance(120)  # 발급 빈도 제한(1분) 이후
    asyncio.run(b.balances())
    assert state["tokens"] == 2 and state["n"] == 2
