"""키움 REST 조회 전용 연결. 주문 TR은 허용 목록에 포함하지 않는다."""

import asyncio
import hashlib
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from aifund.brokers.base import BrokerError, MarketData
from aifund.brokers.ratelimit import GLOBAL_LIMITER
from aifund.core.secrets import KiwoomCreds, register_secret
from aifund.core.timeutil import KST, UTC, SystemClock
from aifund.domain.models import Candle, Instrument, Quote
from aifund.markets.calendar import _calendar

READ_APIS = {
    "usa20101": "/api/us/mrkcond",
    "usa06012": "/api/us/chart",
    "ka10007": "/api/dostk/mrkcond",
    "ka10081": "/api/dostk/chart",
    "kt00001": "/api/dostk/acnt",
    "kt00018": "/api/dostk/acnt",
}
AUTH_CODES = {8003, 8005, 8006, 8009, 8015, 8016, 8031, 8103}


def number(value, *, price=False):
    try:
        n = Decimal(str(value).strip().replace(",", ""))
        if not n.is_finite():
            raise ValueError()
        return abs(n) if price else n
    except (InvalidOperation, ValueError):
        raise BrokerError("invalid", "키움 숫자 필드 누락 또는 형식 오류") from None


class KiwoomReadClient:
    def __init__(self, creds: KiwoomCreds, *, client=None, clock=None, limiter=GLOBAL_LIMITER):
        self.creds = creds
        self.host = "https://mockapi.kiwoom.com" if creds.env == "mock" else "https://api.kiwoom.com"
        self.http = client or httpx.AsyncClient(timeout=20)
        self.owns_http = client is None
        self.clock = clock or SystemClock()
        self.limiter = limiter
        self.scope = "kiwoom:" + creds.env + ":" + hashlib.sha256(creds.app_key.encode()).hexdigest()[:16]
        limiter.configure(self.scope, per_second=0.5, burst=1)
        self.token = None
        self.expires = None
        self.lock = asyncio.Lock()

    async def _post(self, path, body, headers=None):
        for attempt in range(3):
            try:
                return await self._post_once(path, body, headers)
            except BrokerError as exc:
                if exc.kind != "rate_limited" or attempt == 2:
                    raise
                self.limiter.block(self.scope, 2 ** (attempt + 1))

    async def _post_once(self, path, body, headers=None):
        await self.limiter.acquire(self.scope)
        try:
            response = await self.http.post(self.host + path, json=body, headers=headers)
        except httpx.HTTPError:
            raise BrokerError("network", "키움 연결 실패") from None
        if response.status_code != 200:
            kind = {401: "auth", 403: "permission", 429: "rate_limited"}.get(response.status_code, "server")
            if kind == "rate_limited":
                self.limiter.block(self.scope, 2)
            raise BrokerError(kind, f"키움 HTTP {response.status_code}")
        try:
            data = response.json()
            code = data.get("return_code")
            if isinstance(code, bool) or code is None:
                raise ValueError()
            code = int(code)
        except (ValueError, TypeError, AttributeError):
            raise BrokerError("invalid", "키움 응답 형식 오류") from None
        if code:
            embedded = re.search(r"\[(\d{3,5}):|CODE=(\d{3,5})", str(data.get("return_msg", "")))
            if embedded:
                code = int(embedded.group(1) or embedded.group(2))
            kind = "auth" if code in AUTH_CODES else "rate_limited" if code in {1700, 1701, 1702} else "invalid"
            if kind == "rate_limited":
                self.limiter.block(self.scope, 2)
            raise BrokerError(kind, "키움 API 요청 실패", code=str(code))
        return data, response.headers

    async def _authenticate(self):
        async with self.lock:
            if self.token and self.expires and self.expires > self.clock.now() + timedelta(minutes=5):
                return
            data, _ = await self._post("/oauth2/token", {
                "grant_type": "client_credentials", "appkey": self.creds.app_key, "secretkey": self.creds.app_secret,
            })
            try:
                expiry = datetime.strptime(data["expires_dt"], "%Y%m%d%H%M%S").replace(tzinfo=KST).astimezone(UTC)
                token = data["token"]
                if not isinstance(token, str) or not token or expiry <= self.clock.now():
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise BrokerError("auth", "키움 토큰 응답 오류") from None
            register_secret(token)
            self.token, self.expires = token, expiry

    async def call(self, api_id, body, next_key=""):
        if api_id not in READ_APIS:
            raise BrokerError("unsupported", "키움 조회 전용: 허용되지 않은 API")
        for attempt in range(2):
            await self._authenticate()
            try:
                return await self._post(READ_APIS[api_id], body, {
                    "authorization": f"Bearer {self.token}", "api-id": api_id,
                    "cont-yn": "Y" if next_key else "N", "next-key": next_key,
                })
            except BrokerError as exc:
                if exc.kind != "auth" or attempt:
                    raise
                self.token = None

    async def rows(self, api_id, body, field, limit=None):
        rows, key, seen = [], "", set()
        for _ in range(20):
            data, headers = await self.call(api_id, body, key)
            batch = data.get(field)
            if not isinstance(batch, list) or any(not isinstance(r, dict) for r in batch):
                raise BrokerError("invalid", "키움 목록 필드 누락")
            rows.extend(batch)
            if limit is not None and len(rows) >= limit:
                return rows[:limit]
            if headers.get("cont-yn") != "Y":
                return rows
            key = headers.get("next-key", "")
            if not key or key in seen:
                raise BrokerError("invalid", "키움 연속조회 키 오류")
            seen.add(key)
        raise BrokerError("invalid", "키움 연속조회 한도 초과: 불완전 자료 사용 중지")

    async def account_summary(self):
        deposit, _ = await self.call("kt00001", {"qry_tp": "3"})
        rows = await self.rows("kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"}, "acnt_evlt_remn_indv_tot")
        return {"source": "kiwoom", "environment": self.creds.env, "read_only": True,
                "cash_krw": number(deposit.get("entr")), "orderable_krw": number(deposit.get("ord_alow_amt")),
                "holdings": [{"symbol": str(r.get("stk_cd", "")).removeprefix("A"),
                              "name": r.get("stk_nm"), "qty": number(r.get("rmnd_qty")),
                              "average_price": number(r.get("pur_pric"), price=True)} for r in rows]}

    async def close(self):
        if self.owns_http:
            await self.http.aclose()


class KiwoomMarketData(MarketData):
    source_name = "kiwoom"

    def __init__(self, client):
        self.c = client

    async def instruments(self, market, symbols):
        if market != "kr_stock" or any(not re.fullmatch(r"[0-9]{6}", s) for s in symbols):
            raise BrokerError("unsupported", "키움 시세는 국내주식 6자리 종목코드만 지원")
        return [Instrument(f"kr_stock:{s}", market, s, "KRX", s, "KRW", s, "krx", None,
                           Decimal(1), Decimal(1), None, source="kiwoom", meta={"read_only": True}) for s in symbols]

    async def quotes(self, instruments):
        out = []
        for inst in instruments:
            data, _ = await self.c.call("ka10007", {"stk_cd": inst.symbol})
            out.append(Quote(inst.instrument_id, number(data.get("buy_1bid"), price=True) or None,
                             number(data.get("sel_1bid"), price=True) or None, None, None,
                             number(data.get("cur_prc"), price=True), None, None, self.c.clock.now(), "kiwoom"))
        return out

    async def candles(self, instrument, interval, count):
        if interval != "1d":
            raise BrokerError("unsupported", "키움 연결은 일봉(1d) 지원")
        if count <= 0:
            return []
        rows = await self.c.rows("ka10081", {"stk_cd": instrument.symbol,
            "base_dt": self.c.clock.now().astimezone(KST).strftime("%Y%m%d"), "upd_stkpc_tp": "1"}, "stk_dt_pole_chart_qry", limit=count)
        out = {}
        cal = _calendar("kr_stock")
        for r in rows:
            day = datetime.strptime(r["dt"], "%Y%m%d").date().isoformat()
            if not cal.is_session(day):
                continue
            out[day] = Candle(instrument.instrument_id, interval, cal.session_open(day).to_pydatetime(),
                              cal.session_close(day).to_pydatetime(), number(r.get("open_pric"), price=True),
                              number(r.get("high_pric"), price=True), number(r.get("low_pric"), price=True),
                              number(r.get("cur_prc"), price=True), number(r.get("trde_qty")), source="kiwoom")
        return [out[k] for k in sorted(out)][-count:]

    async def close(self):
        await self.c.close()


class KiwoomUSMarketData(MarketData):
    """키움 미국주식 조회. 기존 NASD:/NYSE:/AMEX: 식별자를 유지한다."""

    source_name = "kiwoom_us"
    exchanges = {"NASD": "ND", "NYSE": "NY", "AMEX": "NA"}

    def __init__(self, client):
        self.c = client

    def request_symbol(self, symbol):
        exchange, sep, ticker = symbol.partition(":")
        if not sep or exchange not in self.exchanges or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", ticker):
            raise BrokerError("unsupported", "키움 미국주식 종목은 NASD:NVDA / NYSE:IBM / AMEX:SPY 형식이어야 합니다")
        return {"stex_tp": self.exchanges[exchange], "stk_cd": ticker}

    async def instruments(self, market, symbols):
        if market != "us_stock":
            raise BrokerError("unsupported", "키움 미국주식 시세는 us_stock만 지원")
        out = []
        for symbol in symbols:
            self.request_symbol(symbol)
            exchange, ticker = symbol.split(":")
            out.append(Instrument(f"us_stock:{symbol}", market, symbol, exchange, ticker, "USD", ticker,
                                  "us", None, Decimal(1), Decimal(1), None, source=self.source_name,
                                  meta={"read_only": True}))
        return out

    async def quotes(self, instruments):
        out = []
        for inst in instruments:
            body = self.request_symbol(inst.symbol)
            data, _ = await self.c.call("usa20101", body)
            if data.get("stk_cd") != body["stk_cd"] or data.get("stex_tp") != body["stex_tp"]:
                raise BrokerError("invalid", "키움 미국 시세 응답 종목/거래소 불일치")
            bid, ask = number(data.get("buy_1bid"), price=True), number(data.get("sel_1bid"), price=True)
            if bid > 0 and ask > 0 and bid > ask:
                raise BrokerError("invalid", "키움 미국 시세 매수/매도 호가 역전")
            out.append(Quote(inst.instrument_id, bid or None, ask or None,
                             number(data.get("buy_1bid_req")), number(data.get("sel_1bid_req")),
                             number(data.get("cur_prc"), price=True), None, None, self.c.clock.now(), self.source_name))
        return out

    async def candles(self, instrument, interval, count):
        if interval != "1d":
            raise BrokerError("unsupported", "키움 미국주식 연결은 일봉(1d) 지원")
        if count <= 0:
            return []
        cal = _calendar("us_stock")
        body = {**self.request_symbol(instrument.symbol),
                "strt_dt": self.c.clock.now().astimezone(cal.tz).strftime("%Y%m%d"),
                "upd_stkpc_tp": "1", "exrt_appl_tp": "0"}
        rows = await self.c.rows("usa06012", body, "result_list", limit=count)
        out = {}
        for row in rows:
            day = datetime.strptime(row["dt"], "%Y%m%d").date().isoformat()
            if not cal.is_session(day):
                continue
            out[day] = Candle(instrument.instrument_id, interval, cal.session_open(day).to_pydatetime(),
                              cal.session_close(day).to_pydatetime(), number(row.get("open_pric"), price=True),
                              number(row.get("high_pric"), price=True), number(row.get("low_pric"), price=True),
                              number(row.get("cur_prc"), price=True), number(row.get("acc_trde_qty")), source=self.source_name)
        return [out[day] for day in sorted(out)][-count:]

    async def close(self):
        await self.c.close()
