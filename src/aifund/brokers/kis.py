"""한국투자증권 KIS Open API 어댑터 (github.com/koreainvestment/open-trading-api examples_llm, 2026-09-29 확인).

실전 도메인 https://openapi.koreainvestment.com:9443 / 모의 https://openapivts.koreainvestment.com:29443
토큰: POST /oauth2/tokenP (유효 1일, 재발급 빈도 제한 → 파일 캐시). 헤더: authorization, appkey, appsecret, tr_id, custtype=P.

국내주식 (실전/모의 TR)
  주문    POST /uapi/domestic-stock/v1/trading/order-cash         매수 TTTC0012U/VTTC0012U, 매도 TTTC0011U/VTTC0011U
  취소    POST /uapi/domestic-stock/v1/trading/order-rvsecncl     TTTC0013U/VTTC0013U
  체결조회 GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld   TTTC0081R/VTTC0081R (3개월 이내)
  잔고    GET /uapi/domestic-stock/v1/trading/inquire-balance      TTTC8434R/VTTC8434R
  주문가능 GET /uapi/domestic-stock/v1/trading/inquire-psbl-order   TTTC8908R/VTTC8908R
  미체결  GET /uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl TTTC0084R (실전 전용)
  현재가  GET /uapi/domestic-stock/v1/quotations/inquire-price     FHKST01010100
  호가    GET /uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn FHKST01010200
  일봉    GET /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice FHKST03010100
미국주식
  주문    POST /uapi/overseas-stock/v1/trading/order  매수 TTTT1002U/VTTT1002U, 매도 TTTT1006U/VTTT1001U(*)
  취소    POST /uapi/overseas-stock/v1/trading/order-rvsecncl TTTT1004U/VTTT1004U
  미체결  GET /uapi/overseas-stock/v1/trading/inquire-nccs TTTS3018R
  체결    GET /uapi/overseas-stock/v1/trading/inquire-ccnl TTTS3035R/VTTS3035R (주문번호 검색 불가)
  잔고    GET /uapi/overseas-stock/v1/trading/inquire-balance TTTS3012R/VTTS3012R
  주문가능 GET /uapi/overseas-stock/v1/trading/inquire-psamount TTTS3007R/VTTS3007R
  현재가  GET /uapi/overseas-price/v1/quotations/price HHDFS00000300, 호가 inquire-asking-price HHDFS76200100,
  일봉    GET /uapi/overseas-price/v1/quotations/dailyprice HHDFS76240000
(*) 공식 예제 주석은 모의 미국 매도를 VTTT1001U로 적고 있으나 같은 예제 코드는 'V'+TTTT1006U를 만든다.
    모의투자 검증 전까지 미확정 항목으로 docs/integrations.md에 기록한다.

KIS는 클라이언트 주문 ID를 제공하지 않는다. 주문 응답이 유실되면 당일 주문조회에서 종목·방향·수량·가격·시각으로
유일하게 매칭될 때만 연결하고, 아니면 상태 불명으로 남겨 위험 증가 주문을 막는다.
체결은 누적 수량·금액만 제공 → 누적 증가분으로 체결을 합성한다(수수료는 추정치로 표시).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from aifund.brokers.base import BrokerAdapter, BrokerCapabilities, BrokerError, MarketData, OrderLookupHint
from aifund.brokers.ratelimit import GLOBAL_LIMITER, RateLimiter
from aifund.core.money import D, ZERO, dstr
from aifund.core.secrets import KisCreds
from aifund.core.timeutil import KST, NEW_YORK, UTC, Clock, SystemClock
from aifund.domain.models import (
    AccountCheck,
    Balance,
    BrokerOrderState,
    CancelResult,
    Candle,
    Instrument,
    OrderRequest,
    OrderStatus,
    Quote,
    Side,
    SubmitResult,
)
from aifund.markets.rules import US_ORDER_TO_QUOTE_EXCD

log = logging.getLogger(__name__)

REAL_URL = "https://openapi.koreainvestment.com:9443"
DEMO_URL = "https://openapivts.koreainvestment.com:29443"

TR = {
    ("dom_buy", "real"): "TTTC0012U", ("dom_buy", "demo"): "VTTC0012U",
    ("dom_sell", "real"): "TTTC0011U", ("dom_sell", "demo"): "VTTC0011U",
    ("dom_cancel", "real"): "TTTC0013U", ("dom_cancel", "demo"): "VTTC0013U",
    ("dom_ccld", "real"): "TTTC0081R", ("dom_ccld", "demo"): "VTTC0081R",
    ("dom_balance", "real"): "TTTC8434R", ("dom_balance", "demo"): "VTTC8434R",
    ("dom_psbl", "real"): "TTTC8908R", ("dom_psbl", "demo"): "VTTC8908R",
    ("dom_open", "real"): "TTTC0084R",
    ("us_buy", "real"): "TTTT1002U", ("us_buy", "demo"): "VTTT1002U",
    ("us_sell", "real"): "TTTT1006U", ("us_sell", "demo"): "VTTT1001U",
    ("us_cancel", "real"): "TTTT1004U", ("us_cancel", "demo"): "VTTT1004U",
    ("us_open", "real"): "TTTS3018R",
    ("us_ccnl", "real"): "TTTS3035R", ("us_ccnl", "demo"): "VTTS3035R",
    ("us_balance", "real"): "TTTS3012R", ("us_balance", "demo"): "VTTS3012R",
    ("us_psamount", "real"): "TTTS3007R", ("us_psamount", "demo"): "VTTS3007R",
}
UNVERIFIED_TR = {("us_sell", "demo")}

_TOKEN_EXPIRED_CODES = {"EGW00123", "EGW00121"}
_RATE_CODES = {"EGW00201"}


class KisClient:
    """토큰·요청 제한·헤더를 중앙에서 관리한다(계좌·모드별 1개)."""

    def __init__(
        self,
        creds: KisCreds,
        *,
        token_cache_dir: Path | None,
        client: httpx.AsyncClient | None = None,
        limiter: RateLimiter = GLOBAL_LIMITER,
        clock: Clock | None = None,
        base_url: str | None = None,
    ) -> None:
        self.creds = creds
        self.base_url = base_url or (REAL_URL if creds.env == "real" else DEMO_URL)
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._own = client is None
        self.clock = clock or SystemClock()
        self.limiter = limiter
        self.key = f"kis:{creds.env}:{creds.app_key[-6:]}"
        # 공식 예제의 smart_sleep: 실전 0.05s(≈20/s), 모의 0.5s(≈2/s). 여유를 두고 설정.
        limiter.configure(self.key, 15 if creds.env == "real" else 1.8, 3 if creds.env == "real" else 1)
        self.token_path = (token_cache_dir / f"kis_token_{creds.env}_{creds.app_key[-6:]}.json") if token_cache_dir else None
        self._token: str | None = None
        self._token_exp: datetime | None = None
        self._token_lock = asyncio.Lock()
        self._last_issue_attempt: datetime | None = None

    def _load_cached(self) -> None:
        if self.token_path and self.token_path.exists():
            try:
                d = json.loads(self.token_path.read_text(encoding="utf-8"))
                exp = datetime.fromisoformat(d["expires_at"])
                if exp - timedelta(hours=1) > self.clock.now():
                    self._token, self._token_exp = d["token"], exp
            except (OSError, ValueError, KeyError):
                pass

    async def token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token is None:
                self._load_cached()
            if not force and self._token and self._token_exp and self._token_exp - timedelta(hours=1) > self.clock.now():
                return self._token
            now = self.clock.now()
            if self._last_issue_attempt and (now - self._last_issue_attempt).total_seconds() < 61:
                raise BrokerError("rate_limited", "토큰 발급은 1분에 1회로 제한됩니다", code="token_issue_limit")
            self._last_issue_attempt = now
            body = {"grant_type": "client_credentials", "appkey": self.creds.app_key, "appsecret": self.creds.app_secret}
            try:
                resp = await self._client.post(self.base_url + "/oauth2/tokenP", json=body, headers={"content-type": "application/json"})
            except httpx.HTTPError as exc:
                raise BrokerError("network", f"토큰 발급 실패: {exc.__class__.__name__}") from exc
            if resp.status_code != 200:
                raise BrokerError("auth", f"토큰 발급 실패 HTTP {resp.status_code}", http_status=resp.status_code)
            d = resp.json()
            tok = d.get("access_token")
            if not tok:
                raise BrokerError("auth", "토큰 응답에 access_token이 없습니다")
            exp_s = d.get("access_token_token_expired")
            if exp_s:
                exp = datetime.strptime(exp_s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).astimezone(UTC)
            else:
                exp = now + timedelta(seconds=int(d.get("expires_in", 86400)))
            self._token, self._token_exp = tok, exp
            if self.token_path:
                self.token_path.parent.mkdir(parents=True, exist_ok=True)
                self.token_path.write_text(json.dumps({"token": tok, "expires_at": exp.isoformat()}), encoding="utf-8")
                try:
                    os.chmod(self.token_path, 0o600)
                except OSError:
                    pass
            return tok

    async def call(
        self, method: str, path: str, tr_id: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None,
        tr_cont: str = "", retry_token: bool = True,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        await self.limiter.acquire(self.key)
        tok = await self.token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {tok}",
            "appkey": self.creds.app_key,
            "appsecret": self.creds.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "tr_cont": tr_cont,
        }
        url = self.base_url + path
        if method == "GET":
            resp = await self._client.get(url, headers=headers, params=params)
        else:
            resp = await self._client.post(url, headers=headers, content=json.dumps(body or {}).encode("utf-8"))
        try:
            data = resp.json()
        except ValueError:
            data = {"rt_cd": "-1", "msg1": resp.text[:300]}
        msg_cd = str(data.get("msg_cd", ""))
        if msg_cd in _TOKEN_EXPIRED_CODES and retry_token:
            await self.token(force=True)
            return await self.call(method, path, tr_id, params=params, body=body, tr_cont=tr_cont, retry_token=False)
        if msg_cd in _RATE_CODES or resp.status_code == 429:
            self.limiter.block(self.key, 1.0)
            raise BrokerError("rate_limited", str(data.get("msg1", "")), code=msg_cd, http_status=resp.status_code)
        if resp.status_code >= 500:
            raise BrokerError("server", f"HTTP {resp.status_code}: {data.get('msg1', '')}", code=msg_cd, http_status=resp.status_code)
        if resp.status_code in (401, 403):
            raise BrokerError("auth", str(data.get("msg1", "인증 실패")), code=msg_cd, http_status=resp.status_code)
        if str(data.get("rt_cd")) != "0":
            raise BrokerError("invalid", str(data.get("msg1", "요청 실패")), code=msg_cd, http_status=resp.status_code)
        return data, resp.headers

    async def close(self) -> None:
        if self._own:
            await self._client.aclose()


def _rows(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    v = data.get(key)
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    return []


class KisMarketData(MarketData):
    source_name = "kis"

    def __init__(self, client: KisClient, clock: Clock | None = None) -> None:
        self.c = client
        self.clock = clock or SystemClock()

    async def instruments(self, market: str, symbols: list[str]) -> list[Instrument]:
        out = []
        for sym in symbols:
            if market == "kr_stock":
                data, _ = await self.c.call(
                    "GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                    params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": sym},
                )
                o = data.get("output", {})
                aspr = D(o.get("aspr_unit")) if o.get("aspr_unit") else None
                halted = o.get("temp_stop_yn") == "Y" or o.get("sltr_yn") == "Y"
                warn = o.get("mrkt_warn_cls_code") not in (None, "", "00") or o.get("mang_issu_cls_code") == "Y"
                out.append(
                    Instrument(
                        instrument_id=f"kr_stock:{sym}", market="kr_stock", symbol=sym, exchange="KRX",
                        name=o.get("bstp_kor_isnm"), quote_ccy="KRW", base_asset=sym, tick_policy="krx", fixed_tick=aspr,
                        qty_step=Decimal(1), min_notional=Decimal(1), max_notional=None,
                        status="halted" if halted else "warning" if warn else "active",
                        source="kis:inquire-price", meta={"aspr_unit": str(aspr) if aspr else None},
                    )
                )
            elif market == "us_stock":
                excd, _, ticker = sym.partition(":")
                qexcd = US_ORDER_TO_QUOTE_EXCD.get(excd, excd)
                data, _ = await self.c.call(
                    "GET", "/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                    params={"AUTH": "", "EXCD": qexcd, "SYMB": ticker},
                )
                o = data.get("output", {})
                out.append(
                    Instrument(
                        instrument_id=f"us_stock:{sym}", market="us_stock", symbol=ticker, exchange=excd, name=ticker,
                        quote_ccy="USD", base_asset=ticker, tick_policy="us", fixed_tick=None, qty_step=Decimal(1),
                        min_notional=Decimal("0.01"), max_notional=None,
                        status="active" if o.get("ordy") in (None, "", "매수가능", "Y") else f"ordy:{o.get('ordy')}",
                        source="kis:overseas price", meta={"quote_excd": qexcd, "fractional": False},
                    )
                )
        return out

    async def candles(self, instrument: Instrument, interval: str, count: int) -> list[Candle]:
        if interval != "1d":
            raise BrokerError("unsupported", "주식은 일봉(1d)만 사용합니다")
        today = self.clock.now().astimezone(KST).date()
        out: list[Candle] = []
        if instrument.market == "kr_stock":
            start = today - timedelta(days=int(count * 1.6) + 10)
            data, _ = await self.c.call(
                "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100",
                params={
                    "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": instrument.symbol,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
                },
            )
            for r in _rows(data, "output2"):
                if not r.get("stck_bsop_date"):
                    continue
                d = datetime.strptime(r["stck_bsop_date"], "%Y%m%d")
                o = d.replace(hour=9, tzinfo=KST).astimezone(UTC)
                c = d.replace(hour=15, minute=30, tzinfo=KST).astimezone(UTC)
                out.append(
                    Candle(instrument.instrument_id, "1d", o, c, D(r["stck_oprc"]), D(r["stck_hgpr"]), D(r["stck_lwpr"]),
                           D(r["stck_clpr"]), D(r["acml_vol"]), D(r.get("acml_tr_pbmn")), "kis")
                )
        else:
            qexcd = instrument.meta.get("quote_excd") or US_ORDER_TO_QUOTE_EXCD.get(instrument.exchange or "", "NAS")
            data, _ = await self.c.call(
                "GET", "/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000",
                params={"AUTH": "", "EXCD": qexcd, "SYMB": instrument.symbol, "GUBN": "0", "BYMD": "", "MODP": "1"},
            )
            for r in _rows(data, "output2"):
                if not r.get("xymd"):
                    continue
                d = datetime.strptime(r["xymd"], "%Y%m%d")
                o = d.replace(hour=9, minute=30, tzinfo=NEW_YORK).astimezone(UTC)
                c = d.replace(hour=16, tzinfo=NEW_YORK).astimezone(UTC)
                out.append(
                    Candle(instrument.instrument_id, "1d", o, c, D(r["open"]), D(r["high"]), D(r["low"]), D(r["clos"]),
                           D(r["tvol"]), D(r.get("tamt")), "kis")
                )
        out.sort(key=lambda x: x.open_time)
        return out[-count:]

    async def quotes(self, instruments: list[Instrument]) -> list[Quote]:
        out = []
        now = self.clock.now()
        for inst in instruments:
            if inst.market == "kr_stock":
                data, _ = await self.c.call(
                    "GET", "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn", "FHKST01010200",
                    params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": inst.symbol},
                )
                o1 = data.get("output1", {}) or {}
                o2 = data.get("output2", {}) or {}
                last = o2.get("stck_prpr") or o1.get("stck_prpr")
                out.append(
                    Quote(inst.instrument_id, D(o1.get("bidp1")) or None, D(o1.get("askp1")) or None,
                          D(o1.get("bidp_rsqn1")) or None, D(o1.get("askp_rsqn1")) or None, D(last) or None,
                          None, None, now, "kis")
                )
            else:
                qexcd = inst.meta.get("quote_excd") or "NAS"
                data, _ = await self.c.call(
                    "GET", "/uapi/overseas-price/v1/quotations/inquire-asking-price", "HHDFS76200100",
                    params={"AUTH": "", "EXCD": qexcd, "SYMB": inst.symbol},
                )
                merged: dict[str, Any] = {}
                for k in ("output1", "output2", "output"):
                    for r in _rows(data, k):
                        merged.update({kk: vv for kk, vv in r.items() if vv not in (None, "")})
                out.append(
                    Quote(inst.instrument_id, D(merged.get("pbid1")) or None, D(merged.get("pask1")) or None,
                          D(merged.get("vbid1")) or None, D(merged.get("vask1")) or None, D(merged.get("last")) or None,
                          None, None, now, "kis")
                )
        return out


def _dom_state(rows: list[dict[str, Any]], odno: str) -> BrokerOrderState | None:
    """inquire-daily-ccld 행들에서 주문 상태를 만든다. 취소는 orgn_odno로 별도 행이 생긴다."""
    main = [r for r in rows if str(r.get("odno", "")).lstrip("0") == odno.lstrip("0")]
    if not main:
        return None
    r = main[0]
    ord_qty = D(r.get("ord_qty"))
    ccld = D(r.get("tot_ccld_qty"))
    amt = D(r.get("tot_ccld_amt"))
    rmn = D(r.get("rmn_qty"))
    rjct = D(r.get("rjct_qty"))
    cancel_rows = [x for x in rows if str(x.get("orgn_odno", "")).lstrip("0") == odno.lstrip("0")]
    canceled_qty = D(r.get("cnc_cfrm_qty")) + sum((D(x.get("cnc_cfrm_qty")) for x in cancel_rows), ZERO)
    if ccld >= ord_qty and ord_qty > 0:
        status = OrderStatus.FILLED
    elif rjct > 0 and rjct + ccld >= ord_qty:
        status = OrderStatus.REJECTED if ccld == 0 else OrderStatus.CANCELED
    elif (r.get("cncl_yn") == "Y" or canceled_qty > 0) and rmn == 0:
        status = OrderStatus.CANCELED
    elif ccld > 0:
        status = OrderStatus.PARTIALLY_FILLED
    else:
        status = OrderStatus.SUBMITTED
    return BrokerOrderState(
        broker_order_id=str(r.get("odno")), client_order_id=None, status=status, filled_qty=ccld,
        filled_amount=amt, fee_total=None, trades=None, raw_state=f"ccld={ccld} rmn={rmn} cncl={r.get('cncl_yn')}",
        instrument_id=f"kr_stock:{r.get('pdno')}", side=Side.SELL if r.get("sll_buy_dvsn_cd") == "01" else Side.BUY,
        qty=ord_qty, limit_price=D(r.get("ord_unpr")),
        meta={"ord_tmd": r.get("ord_tmd"), "ord_gno_brno": r.get("ord_gno_brno"), "ord_dt": r.get("ord_dt")},
    )


def _us_state(r: dict[str, Any]) -> BrokerOrderState:
    ord_qty = D(r.get("ft_ord_qty"))
    ccld = D(r.get("ft_ccld_qty"))
    nccs = D(r.get("nccs_qty"))
    amt = D(r.get("ft_ccld_amt3"))
    stat = str(r.get("prcs_stat_name", ""))
    rj = str(r.get("rjct_rson", "") or r.get("rjct_rson_name", "") or "").strip()
    if ccld >= ord_qty and ord_qty > 0:
        status = OrderStatus.FILLED
    elif rj and ccld == 0:
        status = OrderStatus.REJECTED
    elif "취소" in stat and nccs == 0:
        status = OrderStatus.CANCELED
    elif ccld > 0:
        status = OrderStatus.PARTIALLY_FILLED
    else:
        status = OrderStatus.SUBMITTED
    return BrokerOrderState(
        broker_order_id=str(r.get("odno")), client_order_id=None, status=status, filled_qty=ccld, filled_amount=amt,
        fee_total=None, trades=None, raw_state=stat or f"ccld={ccld} nccs={nccs}",
        instrument_id=None, side=Side.SELL if r.get("sll_buy_dvsn_cd") == "01" else Side.BUY, qty=ord_qty,
        limit_price=D(r.get("ft_ord_unpr3")), meta={"ord_tmd": r.get("ord_tmd"), "ovrs_excg_cd": r.get("ovrs_excg_cd")},
    )


class KisBroker(BrokerAdapter):
    """국내·미국 주식 공용. market에 따라 엔드포인트를 선택한다."""

    name = "kis"

    def __init__(self, account_id: str, market: str, client: KisClient, *, live_money: bool, clock: Clock | None = None) -> None:
        super().__init__(account_id)
        if market not in ("kr_stock", "us_stock"):
            raise ValueError(market)
        self.market = market
        self.c = client
        self.env = client.creds.env
        self.is_live_money = live_money and self.env == "real"
        self.clock = clock or SystemClock()
        if not client.creds.account_no:
            raise BrokerError("auth", "KIS 계좌번호(ACCOUNT_NO)가 설정되지 않았습니다")

    def tr(self, key: str) -> str:
        v = TR.get((key, self.env))
        if v is None:
            raise BrokerError("unsupported", f"{self.env} 환경에서 지원되지 않는 조회입니다: {key}")
        return v

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            quotes=True, candles=True, instrument_meta=True, balances=True, orderable_cash=True, place_order=True,
            order_test=False, cancel_order=True, client_order_id=False, open_orders=self.env == "real",
            per_trade_fills=False, exchange_fees_reported=False, server_side_stop=False, sandbox=self.env == "demo",
            notes=(
                "클라이언트 주문 ID 미지원 → 응답 유실 시 당일 주문조회로 유일 매칭될 때만 연결",
                "체결은 누적 수량·금액만 제공 → 수수료는 설정 요율로 추정",
                "미체결 조회(TTTC0084R/TTTS3018R)는 실전 전용",
                "소수점 주식 주문·자동 환전은 가정하지 않음",
            ),
        )

    def _acct(self) -> dict[str, str]:
        return {"CANO": self.c.creds.account_no, "ACNT_PRDT_CD": self.c.creds.account_product}

    async def check_account(self) -> AccountCheck:
        try:
            bals = await self.balances()
        except BrokerError as e:
            return AccountCheck(ok=False, auth_ok=e.kind != "auth", detail={"code": e.code}, error=str(e))
        except httpx.HTTPError as exc:
            return AccountCheck(ok=False, auth_ok=False, detail={}, error=f"네트워크 오류: {exc.__class__.__name__}")
        return AccountCheck(ok=True, auth_ok=True, detail={"assets": len(bals), "env": self.env})

    async def balances(self) -> dict[str, Balance]:
        out: dict[str, Balance] = {}
        if self.market == "kr_stock":
            data, _ = await self.c.call(
                "GET", "/uapi/domestic-stock/v1/trading/inquire-balance", self.tr("dom_balance"),
                params={**self._acct(), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
                        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                        "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
            )
            for r in _rows(data, "output1"):
                qty = D(r.get("hldg_qty"))
                if qty > 0:
                    free = D(r.get("ord_psbl_qty"))
                    out[f"kr_stock:{r['pdno']}"] = Balance(f"kr_stock:{r['pdno']}", free, qty - free)
            o2 = _rows(data, "output2")
            if o2:
                out["KRW"] = Balance("KRW", D(o2[0].get("dnca_tot_amt")), ZERO)
        else:
            data, _ = await self.c.call(
                "GET", "/uapi/overseas-stock/v1/trading/inquire-balance", self.tr("us_balance"),
                params={**self._acct(), "OVRS_EXCG_CD": "NASD", "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""},
            )
            for r in _rows(data, "output1"):
                qty = D(r.get("ovrs_cblc_qty"))
                if qty > 0:
                    free = D(r.get("ord_psbl_qty"))
                    key = f"us_stock:{r.get('ovrs_excg_cd', 'NASD')}:{r['ovrs_pdno']}"
                    out[key] = Balance(key, free, qty - free)
        return out

    async def orderable_cash(self, instrument: Instrument, price: Decimal) -> Decimal:
        if self.market == "kr_stock":
            data, _ = await self.c.call(
                "GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order", self.tr("dom_psbl"),
                params={**self._acct(), "PDNO": instrument.symbol, "ORD_UNPR": dstr(price), "ORD_DVSN": "01",
                        "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"},
            )
            o = data.get("output", {})
            # 미수 없는 매수 금액을 사용(신용·미수 금지)
            return D(o.get("nrcvb_buy_amt"))
        data, _ = await self.c.call(
            "GET", "/uapi/overseas-stock/v1/trading/inquire-psamount", self.tr("us_psamount"),
            params={**self._acct(), "OVRS_EXCG_CD": instrument.exchange or "NASD", "OVRS_ORD_UNPR": dstr(price),
                    "ITEM_CD": instrument.symbol},
        )
        o = data.get("output", {})
        # 환전 없이 보유 외화로 주문 가능한 금액만 사용(자동 환전 가정 금지)
        return D(o.get("ord_psbl_frcr_amt"))

    async def submit_order(self, req: OrderRequest) -> SubmitResult:
        if req.order_type != "limit":
            return SubmitResult.rejected("unsupported", "지정가 주문만 사용합니다")
        qty = req.qty
        if qty != qty.to_integral_value():
            return SubmitResult.rejected("fractional", "소수점 주식 주문은 지원하지 않습니다")
        try:
            if self.market == "kr_stock":
                key = "dom_buy" if req.side == Side.BUY else "dom_sell"
                body = {
                    **self._acct(), "PDNO": req.instrument.symbol, "ORD_DVSN": "00", "ORD_QTY": str(int(qty)),
                    "ORD_UNPR": str(int(req.limit_price)), "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if req.side == Side.SELL else "",
                    "CNDT_PRIC": "",
                }
                data, _ = await self.c.call("POST", "/uapi/domestic-stock/v1/trading/order-cash", self.tr(key), body=body)
            else:
                key = "us_buy" if req.side == Side.BUY else "us_sell"
                body = {
                    **self._acct(), "OVRS_EXCG_CD": req.instrument.exchange or "NASD", "PDNO": req.instrument.symbol,
                    "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": dstr(req.limit_price), "CTAC_TLNO": "", "MGCO_APTM_ODNO": "",
                    "SLL_TYPE": "00" if req.side == Side.SELL else "", "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
                }
                data, _ = await self.c.call("POST", "/uapi/overseas-stock/v1/trading/order", self.tr(key), body=body)
        except BrokerError as e:
            if e.kind in ("invalid", "auth", "rate_limited", "unsupported"):
                return SubmitResult.rejected(e.code or e.kind, e.message)
            return SubmitResult.unknown(e.code or e.kind, e.message)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return SubmitResult.rejected("not_sent", exc.__class__.__name__)
        except httpx.HTTPError as exc:
            return SubmitResult.unknown("no_response", exc.__class__.__name__)
        o = data.get("output", {}) or {}
        odno = o.get("ODNO") or o.get("odno")
        if not odno:
            return SubmitResult.unknown("no_odno", "주문번호가 응답에 없습니다")
        return SubmitResult.accepted(str(odno), orgno=o.get("KRX_FWDG_ORD_ORGNO"), ord_tmd=o.get("ORD_TMD"))

    async def _dom_rows(self, day: str, pdno: str = "", odno: str = "") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        fk, nk, cont = "", "", ""
        for _ in range(10):
            data, headers = await self.c.call(
                "GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", self.tr("dom_ccld"),
                params={**self._acct(), "INQR_STRT_DT": day, "INQR_END_DT": day, "SLL_BUY_DVSN_CD": "00", "PDNO": pdno,
                        "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": odno,
                        "INQR_DVSN_1": "", "CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk, "EXCG_ID_DVSN_CD": "KRX"},
                tr_cont=cont,
            )
            rows.extend(_rows(data, "output1"))
            if headers.get("tr_cont") in ("F", "M"):
                fk, nk, cont = data.get("ctx_area_fk100", ""), data.get("ctx_area_nk100", ""), "N"
            else:
                break
        return rows

    async def _us_rows(self, day: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        fk, nk, cont = "", "", ""
        demo = self.env == "demo"
        for _ in range(10):
            data, headers = await self.c.call(
                "GET", "/uapi/overseas-stock/v1/trading/inquire-ccnl", self.tr("us_ccnl"),
                params={**self._acct(), "PDNO": "" if demo else "%", "ORD_STRT_DT": day, "ORD_END_DT": day,
                        "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00", "OVRS_EXCG_CD": "" if demo else "%",
                        "SORT_SQN": "DS", "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "", "CTX_AREA_NK200": nk,
                        "CTX_AREA_FK200": fk},
                tr_cont=cont,
            )
            rows.extend(_rows(data, "output"))
            if headers.get("tr_cont") in ("F", "M"):
                fk, nk, cont = data.get("ctx_area_fk200", ""), data.get("ctx_area_nk200", ""), "N"
            else:
                break
        return rows

    def _local_day(self, when: datetime | None = None) -> str:
        now = when or self.clock.now()
        tz = KST if self.market == "kr_stock" else NEW_YORK
        return now.astimezone(tz).strftime("%Y%m%d")

    async def get_order(
        self, *, broker_order_id: str | None, client_order_id: str, hint: OrderLookupHint | None = None
    ) -> BrokerOrderState | None:
        day = self._local_day(hint.submitted_after if hint else None)
        if broker_order_id:
            if self.market == "kr_stock":
                rows = await self._dom_rows(day, odno=broker_order_id)
                if not rows:
                    rows = await self._dom_rows(day)
                return _dom_state(rows, broker_order_id)
            rows = await self._us_rows(day)
            for r in rows:
                if str(r.get("odno", "")).lstrip("0") == broker_order_id.lstrip("0"):
                    return _us_state(r)
            return None
        if hint is None:
            raise BrokerError("unsupported", "KIS는 클라이언트 주문 ID로 조회할 수 없습니다(단서 필요)")
        # 응답 유실 주문 매칭: 종목·방향·수량·가격·시각이 모두 맞고 유일해야 한다.
        side_cd = "01" if hint.side == "sell" else "02"
        cands = []
        if self.market == "kr_stock":
            rows = await self._dom_rows(day, pdno=hint.instrument.symbol)
            for r in rows:
                if r.get("orgn_odno") and str(r.get("orgn_odno")).strip("0"):
                    continue  # 취소/정정 행 제외
                if r.get("sll_buy_dvsn_cd") != side_cd or D(r.get("ord_qty")) != hint.qty:
                    continue
                if D(r.get("ord_unpr")) != hint.limit_price or str(r.get("odno")) in hint.exclude_broker_ids:
                    continue
                if not _tmd_after(r.get("ord_tmd"), day, KST, hint.submitted_after):
                    continue
                cands.append(r)
            if len(cands) == 1:
                return _dom_state(rows, str(cands[0]["odno"]))
        else:
            rows = await self._us_rows(day)
            for r in rows:
                if r.get("pdno") != hint.instrument.symbol or r.get("sll_buy_dvsn_cd") != side_cd:
                    continue
                if D(r.get("ft_ord_qty")) != hint.qty or D(r.get("ft_ord_unpr3")) != hint.limit_price:
                    continue
                if str(r.get("odno")) in hint.exclude_broker_ids:
                    continue
                cands.append(r)
            if len(cands) == 1:
                return _us_state(cands[0])
        if len(cands) > 1:
            raise BrokerError("unknown", f"응답 유실 주문 후보가 {len(cands)}건이라 자동 연결할 수 없습니다", code="ambiguous")
        return None

    async def cancel_order(self, *, broker_order_id: str | None, client_order_id: str, instrument: Instrument) -> CancelResult:
        if not broker_order_id:
            return CancelResult(accepted=False, error_code="no_broker_id", error_message="거래소 주문번호가 없어 취소할 수 없습니다")
        try:
            if self.market == "kr_stock":
                st = await self.get_order(broker_order_id=broker_order_id, client_order_id=client_order_id)
                orgno = (st.meta.get("ord_gno_brno") if st else None) or ""
                body = {**self._acct(), "KRX_FWDG_ORD_ORGNO": orgno, "ORGN_ODNO": broker_order_id, "ORD_DVSN": "00",
                        "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y",
                        "EXCG_ID_DVSN_CD": "KRX"}
                await self.c.call("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", self.tr("dom_cancel"), body=body)
            else:
                body = {**self._acct(), "OVRS_EXCG_CD": instrument.exchange or "NASD", "PDNO": instrument.symbol,
                        "ORGN_ODNO": broker_order_id, "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "OVRS_ORD_UNPR": "0",
                        "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0"}
                await self.c.call("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl", self.tr("us_cancel"), body=body)
        except BrokerError as e:
            return CancelResult(accepted=False, error_code=e.code or e.kind, error_message=e.message)
        except httpx.HTTPError as exc:
            return CancelResult(accepted=False, error_code="network", error_message=exc.__class__.__name__)
        return CancelResult(accepted=True)

    async def open_orders(self, instruments: list[Instrument]) -> list[BrokerOrderState]:
        if self.market == "kr_stock":
            if self.env != "real":
                rows = await self._dom_rows(self._local_day())
                states = [_dom_state(rows, str(r.get("odno"))) for r in rows if not str(r.get("orgn_odno", "")).strip("0")]
                return [s for s in states if s and s.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)]
            data, _ = await self.c.call(
                "GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl", self.tr("dom_open"),
                params={**self._acct(), "INQR_DVSN_1": "0", "INQR_DVSN_2": "0", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
            )
            out = []
            for r in _rows(data, "output"):
                ccld = D(r.get("tot_ccld_qty"))
                out.append(BrokerOrderState(
                    broker_order_id=str(r.get("odno")), client_order_id=None,
                    status=OrderStatus.PARTIALLY_FILLED if ccld > 0 else OrderStatus.SUBMITTED, filled_qty=ccld,
                    filled_amount=D(r.get("tot_ccld_amt")), fee_total=None, trades=None, raw_state="open",
                    instrument_id=f"kr_stock:{r.get('pdno')}", side=Side.SELL if r.get("sll_buy_dvsn_cd") == "01" else Side.BUY,
                    qty=D(r.get("ord_qty")), limit_price=D(r.get("ord_unpr")),
                ))
            return out
        if self.env != "real":
            rows = await self._us_rows(self._local_day())
            return [s for s in (_us_state(r) for r in rows) if s.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)]
        data, _ = await self.c.call(
            "GET", "/uapi/overseas-stock/v1/trading/inquire-nccs", self.tr("us_open"),
            params={**self._acct(), "OVRS_EXCG_CD": "NASD", "SORT_SQN": "DS", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""},
        )
        out = []
        for r in _rows(data, "output"):
            st = _us_state(r)
            st.instrument_id = f"us_stock:{r.get('ovrs_excg_cd', 'NASD')}:{r.get('pdno')}"
            out.append(st)
        return out

    async def close(self) -> None:
        await self.c.close()


def _tmd_after(tmd: str | None, day: str, tz: Any, after: datetime) -> bool:
    if not tmd:
        return True
    try:
        t = datetime.strptime(day + str(tmd).zfill(6), "%Y%m%d%H%M%S").replace(tzinfo=tz)
    except ValueError:
        return True
    return t >= after.astimezone(tz).replace(microsecond=0) - timedelta(seconds=5)
