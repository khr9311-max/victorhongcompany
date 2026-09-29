"""업비트 어댑터 (docs.upbit.com/kr, 2026-09-29 확인).

- 인증: JWT HS512, payload = access_key, nonce(UUID), query_hash(SHA512 of unquoted query string).
- 주문: POST /v1/orders (ord_type=limit, identifier=클라이언트 주문 ID → 멱등 조회·취소 가능).
- 주문 검증: POST /v1/orders/test (실제 주문 미생성).
- 조회: GET /v1/order (uuid|identifier), GET /v1/orders/open, GET /v1/accounts, GET /v1/orders/chance.
- 요청 제한: 시세 그룹별 초당 10회(IP), Exchange default 초당 30회·order 초당 12회(포켓 단위).
- 공식 테스트넷은 확인되지 않음 → broker_sandbox 미지원.
- 거래소 측 손절(보호) 주문 API는 확인되지 않음 → server_side_stop=False.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlencode

import httpx
import jwt

from aifund.brokers.base import BrokerAdapter, BrokerCapabilities, BrokerError, MarketData, OrderLookupHint
from aifund.brokers.ratelimit import GLOBAL_LIMITER, RateLimiter, parse_upbit_remaining
from aifund.core.money import D, ZERO, dstr
from aifund.core.secrets import UpbitCreds
from aifund.core.timeutil import UTC, Clock, SystemClock, parse_iso
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
    TradeFill,
)
from aifund.markets.rules import UPBIT_KRW_MIN_ORDER, UPBIT_VOLUME_STEP

log = logging.getLogger(__name__)

BASE_URL = "https://api.upbit.com"

# 요청이 처리되어 거절된 것이 확실한 오류(주문 미생성).
_DEFINITE_REJECT_STATUS = {400, 401, 403, 404, 422}
_AUTH_ERRORS = {"jwt_verification", "expired_access_key", "nonce_used", "no_authorization_ip", "no_authorization_token", "invalid_query_payload"}


def build_query(params: dict[str, Any] | None) -> tuple[str, str]:
    """(URL용 인코딩 문자열, 해시용 비인코딩 문자열)."""
    if not params:
        return "", ""
    encoded = urlencode(params, doseq=True)
    return encoded, unquote(encoded)


def make_jwt(creds: UpbitCreds, hash_source: str) -> str:
    payload: dict[str, Any] = {"access_key": creds.access_key, "nonce": str(uuid.uuid4())}
    if hash_source:
        payload["query_hash"] = hashlib.sha512(hash_source.encode("utf-8")).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    token = jwt.encode(payload, creds.secret_key, algorithm="HS512")
    return token if isinstance(token, str) else token.decode("utf-8")


class UpbitHttp:
    def __init__(
        self,
        *,
        creds: UpbitCreds | None = None,
        scope: str = "public",
        client: httpx.AsyncClient | None = None,
        limiter: RateLimiter = GLOBAL_LIMITER,
        clock: Clock | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.creds = creds
        self.scope = scope
        self.base_url = base_url
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._own_client = client is None
        self.limiter = limiter
        self.clock = clock or SystemClock()
        self.last_clock_skew: float | None = None
        for g in ("market", "candle", "ticker", "orderbook", "trade"):
            limiter.configure(f"upbit:ip:{g}", 8, 8)
        limiter.configure(f"upbit:{scope}:default", 20, 20)
        limiter.configure(f"upbit:{scope}:order", 8, 8)
        limiter.configure(f"upbit:{scope}:order-test", 5, 5)

    def _limit_key(self, group: str) -> str:
        if group in ("market", "candle", "ticker", "orderbook", "trade"):
            return f"upbit:ip:{group}"
        return f"upbit:{self.scope}:{group}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
        group: str = "default",
    ) -> tuple[int, Any, httpx.Headers]:
        key = self._limit_key(group)
        await self.limiter.acquire(key)
        headers = {"Accept": "application/json"}
        url = self.base_url + path
        if method in ("GET", "DELETE"):
            encoded, raw = build_query(params)
            if encoded:
                url += "?" + encoded
            content = None
            hash_src = raw
        else:
            _, hash_src = build_query(body)
            content = json.dumps(body or {}, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if auth:
            if self.creds is None:
                raise BrokerError("auth", "업비트 API 키가 설정되지 않았습니다")
            headers["Authorization"] = "Bearer " + make_jwt(self.creds, hash_src)
        resp = await self._client.request(method, url, headers=headers, content=content)
        self._update_skew(resp)
        grp, sec = parse_upbit_remaining(resp.headers.get("Remaining-Req"))
        if sec is not None:
            self.limiter.update_remaining(key, sec)
        if resp.status_code == 429:
            self.limiter.block(key, 1.5)
        elif resp.status_code == 418:
            self.limiter.block(key, 60.0)
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        return resp.status_code, data, resp.headers

    def _update_skew(self, resp: httpx.Response) -> None:
        date_h = resp.headers.get("Date")
        if not date_h:
            return
        try:
            server = parsedate_to_datetime(date_h).astimezone(UTC)
        except (TypeError, ValueError):
            return
        self.last_clock_skew = (self.clock.now() - server).total_seconds()

    async def close(self) -> None:
        if self._own_client:
            await self._client.aclose()


def _err(status: int, data: Any) -> BrokerError:
    err = data.get("error", {}) if isinstance(data, dict) else {}
    name = str(err.get("name", status))
    msg = str(err.get("message", ""))[:300]
    if status == 429:
        return BrokerError("rate_limited", msg or "요청 한도 초과", code=name, http_status=status)
    if status == 418:
        return BrokerError("blocked", msg or "과도한 요청으로 일시 차단", code=name, http_status=status)
    if status == 401 or name in _AUTH_ERRORS:
        return BrokerError("auth", msg or "인증 실패", code=name, http_status=status)
    if name == "out_of_scope":
        return BrokerError("permission", msg or "API 키 권한 부족", code=name, http_status=status)
    if name == "market_offline":
        return BrokerError("market_offline", msg, code=name, http_status=status)
    if name.startswith("insufficient_funds"):
        return BrokerError("insufficient", msg, code=name, http_status=status)
    if name in ("order_not_found", "notfoundmarket") or status == 404:
        return BrokerError("not_found", msg, code=name, http_status=status)
    if name == "duplicated_identifier":
        return BrokerError("duplicate", msg, code=name, http_status=status)
    if status >= 500:
        return BrokerError("server", msg or "거래소 서버 오류", code=name, http_status=status)
    return BrokerError("invalid", msg, code=name, http_status=status)


def _order_state(o: dict[str, Any]) -> BrokerOrderState:
    state = o.get("state")
    executed = D(o.get("executed_volume"))
    if state in ("wait", "watch"):
        status = OrderStatus.PARTIALLY_FILLED if executed > 0 else OrderStatus.SUBMITTED
    elif state == "done":
        status = OrderStatus.FILLED
    elif state == "cancel":
        status = OrderStatus.CANCELED
    else:
        status = OrderStatus.UNKNOWN
    trades_raw = o.get("trades")
    trades: list[TradeFill] | None = None
    amount: Decimal | None = None
    if isinstance(trades_raw, list):
        trades = []
        amount = ZERO
        for t in trades_raw:
            qty = D(t.get("volume"))
            price = D(t.get("price"))
            funds = D(t.get("funds")) if t.get("funds") is not None else qty * price
            amount += funds
            ts = t.get("created_at")
            trades.append(TradeFill(trade_id=str(t.get("uuid")), qty=qty, price=price, fee=None, ts=parse_iso(ts) if ts else None))
    side = Side.BUY if o.get("side") == "bid" else Side.SELL if o.get("side") == "ask" else None
    return BrokerOrderState(
        broker_order_id=o.get("uuid"),
        client_order_id=o.get("identifier"),
        status=status,
        filled_qty=executed,
        filled_amount=amount,
        fee_total=D(o.get("paid_fee")) if o.get("paid_fee") is not None else None,
        trades=trades,
        raw_state=str(state),
        instrument_id=f"crypto:{o.get('market')}" if o.get("market") else None,
        side=side,
        qty=D(o.get("volume")) if o.get("volume") is not None else None,
        limit_price=D(o.get("price")) if o.get("price") is not None else None,
        meta={"created_at": o.get("created_at"), "trades_count": o.get("trades_count")},
    )


class UpbitMarketData(MarketData):
    source_name = "upbit_public"

    def __init__(self, http: UpbitHttp | None = None, clock: Clock | None = None) -> None:
        self.http = http or UpbitHttp(scope="public", clock=clock)
        self.clock = clock or SystemClock()

    async def instruments(self, market: str, symbols: list[str]) -> list[Instrument]:
        if market != "crypto":
            raise BrokerError("unsupported", "업비트는 코인 시장만 지원합니다")
        status, data, _ = await self.http.request("GET", "/v1/market/all", params={"isDetails": "true"}, group="market")
        if status != 200:
            raise _err(status, data)
        by_sym = {d["market"]: d for d in data}
        wanted = [s for s in symbols]
        ticks: dict[str, Decimal] = {}
        if wanted:
            st2, d2, _ = await self.http.request(
                "GET", "/v1/orderbook/instruments", params={"markets": ",".join(wanted)}, group="orderbook"
            )
            if st2 == 200:
                ticks = {x["market"]: D(x["tick_size"]) for x in d2}
        out: list[Instrument] = []
        for sym in wanted:
            d = by_sym.get(sym)
            if d is None:
                out.append(
                    Instrument(
                        instrument_id=f"crypto:{sym}", market="crypto", symbol=sym, exchange="UPBIT", name=None,
                        quote_ccy="KRW", base_asset=sym.split("-")[-1], tick_policy="upbit_krw", fixed_tick=None,
                        qty_step=UPBIT_VOLUME_STEP, min_notional=UPBIT_KRW_MIN_ORDER, max_notional=None,
                        status="not_listed", source="upbit:/v1/market/all",
                    )
                )
                continue
            if not sym.startswith("KRW-"):
                status_s = "unsupported_quote"
            else:
                ev = d.get("market_event") or {}
                warn = bool(ev.get("warning")) or d.get("market_warning") == "CAUTION"
                caution = ev.get("caution") or {}
                status_s = "warning" if warn or any(bool(v) for v in caution.values()) else "active"
            out.append(
                Instrument(
                    instrument_id=f"crypto:{sym}", market="crypto", symbol=sym, exchange="UPBIT",
                    name=d.get("korean_name"), quote_ccy="KRW", base_asset=sym.split("-")[-1],
                    tick_policy="upbit_krw", fixed_tick=ticks.get(sym), qty_step=UPBIT_VOLUME_STEP,
                    min_notional=UPBIT_KRW_MIN_ORDER, max_notional=None, status=status_s,
                    source="upbit:/v1/market/all+/v1/orderbook/instruments; 최소주문=docs krw-market-info",
                    meta={"api_tick_size": str(ticks.get(sym)) if sym in ticks else None, "market_event": d.get("market_event")},
                )
            )
        return out

    async def candles(self, instrument: Instrument, interval: str, count: int) -> list[Candle]:
        count = max(1, min(200, count))
        if interval == "1d":
            path, minutes = "/v1/candles/days", 1440
        else:
            minutes = int(interval[:-1])
            path = f"/v1/candles/minutes/{minutes}"
        status, data, _ = await self.http.request(
            "GET", path, params={"market": instrument.symbol, "count": count}, group="candle"
        )
        if status != 200:
            raise _err(status, data)
        out: list[Candle] = []
        for c in data:
            start = datetime.fromisoformat(c["candle_date_time_utc"]).replace(tzinfo=UTC)
            out.append(
                Candle(
                    instrument_id=instrument.instrument_id, interval=interval, open_time=start,
                    close_time=start + timedelta(minutes=minutes), open=D(c["opening_price"]), high=D(c["high_price"]),
                    low=D(c["low_price"]), close=D(c["trade_price"]), volume=D(c["candle_acc_trade_volume"]),
                    value=D(c.get("candle_acc_trade_price")), source="upbit",
                )
            )
        out.sort(key=lambda x: x.open_time)
        return out

    async def history(self, instrument: Instrument, interval: str, total: int) -> list[Candle]:
        """과거 캔들 페이지 조회(`to` = 마지막 캔들 시각, exclusive). 요청 제한은 중앙 제한기가 관리."""
        minutes = 1440 if interval == "1d" else int(interval[:-1])
        path = "/v1/candles/days" if interval == "1d" else f"/v1/candles/minutes/{minutes}"
        out: dict[datetime, Candle] = {}
        to: str | None = None
        while len(out) < total:
            params: dict[str, Any] = {"market": instrument.symbol, "count": min(200, total - len(out))}
            if to:
                params["to"] = to
            status, data, _ = await self.http.request("GET", path, params=params, group="candle")
            if status != 200:
                raise _err(status, data)
            if not data:
                break
            for c in data:
                start = datetime.fromisoformat(c["candle_date_time_utc"]).replace(tzinfo=UTC)
                out[start] = Candle(instrument.instrument_id, interval, start, start + timedelta(minutes=minutes),
                                    D(c["opening_price"]), D(c["high_price"]), D(c["low_price"]), D(c["trade_price"]),
                                    D(c["candle_acc_trade_volume"]), D(c.get("candle_acc_trade_price")), "upbit")
            oldest = min(datetime.fromisoformat(c["candle_date_time_utc"]) for c in data)
            to = oldest.strftime("%Y-%m-%dT%H:%M:%SZ")
            if len(data) < params["count"]:
                break
        return sorted(out.values(), key=lambda x: x.open_time)

    async def quotes(self, instruments: list[Instrument]) -> list[Quote]:
        if not instruments:
            return []
        syms = ",".join(i.symbol for i in instruments)
        st1, tick, _ = await self.http.request("GET", "/v1/ticker", params={"markets": syms}, group="ticker")
        if st1 != 200:
            raise _err(st1, tick)
        st2, books, _ = await self.http.request("GET", "/v1/orderbook", params={"markets": syms}, group="orderbook")
        if st2 != 200:
            raise _err(st2, books)
        now = self.clock.now()
        tmap = {t["market"]: t for t in tick}
        bmap = {b["market"]: b for b in books}
        out: list[Quote] = []
        for inst in instruments:
            t = tmap.get(inst.symbol, {})
            b = bmap.get(inst.symbol, {})
            units = b.get("orderbook_units") or [{}]
            u0 = units[0]
            ts_ms = b.get("timestamp") or t.get("trade_timestamp")
            out.append(
                Quote(
                    instrument_id=inst.instrument_id,
                    bid=D(u0["bid_price"]) if "bid_price" in u0 else None,
                    ask=D(u0["ask_price"]) if "ask_price" in u0 else None,
                    bid_size=D(u0["bid_size"]) if "bid_size" in u0 else None,
                    ask_size=D(u0["ask_size"]) if "ask_size" in u0 else None,
                    last=D(t["trade_price"]) if "trade_price" in t else None,
                    turnover_24h=D(t["acc_trade_price_24h"]) if "acc_trade_price_24h" in t else None,
                    ts_exchange=datetime.fromtimestamp(ts_ms / 1000, UTC) if ts_ms else None,
                    fetched_at=now,
                    source="upbit",
                )
            )
        return out

    async def close(self) -> None:
        await self.http.close()


class UpbitBroker(BrokerAdapter):
    name = "upbit"

    def __init__(self, account_id: str, creds: UpbitCreds, *, live_money: bool, http: UpbitHttp | None = None, clock: Clock | None = None) -> None:
        super().__init__(account_id)
        self.clock = clock or SystemClock()
        self.http = http or UpbitHttp(creds=creds, scope=account_id, clock=self.clock)
        self.is_live_money = live_money
        self._fees: dict[str, Decimal] = {}

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            quotes=True, candles=True, instrument_meta=True, balances=True, orderable_cash=True, place_order=True,
            order_test=True, cancel_order=True, client_order_id=True, open_orders=True, per_trade_fills=True,
            exchange_fees_reported=True, server_side_stop=False, sandbox=False,
            notes=(
                "공식 테스트넷 미확인 → 모의는 내부 paper만 사용",
                "거래소 측 손절 주문 API 미확인 → 손절은 로컬 감시(맥북·네트워크가 꺼지면 동작 안 함)",
                "API 키는 서브포켓 전용으로 발급하고 자산조회·주문조회·주문하기 권한만 부여 권장",
            ),
        )

    async def check_account(self) -> AccountCheck:
        try:
            status, data, _ = await self.http.request("GET", "/v1/accounts", auth=True)
        except httpx.HTTPError as exc:
            return AccountCheck(ok=False, auth_ok=False, detail={}, error=f"네트워크 오류: {exc.__class__.__name__}")
        if status != 200:
            e = _err(status, data)
            return AccountCheck(ok=False, auth_ok=e.kind not in ("auth", "permission"), detail={"code": e.code}, error=str(e))
        detail: dict[str, Any] = {"assets": len(data)}
        try:
            st2, keys, _ = await self.http.request("GET", "/v1/api_keys", auth=True)
            if st2 == 200:
                detail["api_key_expiry"] = [k.get("expire_at") for k in keys]
        except httpx.HTTPError:
            pass
        return AccountCheck(ok=True, auth_ok=True, detail=detail, clock_skew_sec=self.http.last_clock_skew)

    async def balances(self) -> dict[str, Balance]:
        status, data, _ = await self.http.request("GET", "/v1/accounts", auth=True)
        if status != 200:
            raise _err(status, data)
        return {d["currency"]: Balance(d["currency"], D(d["balance"]), D(d["locked"])) for d in data}

    async def _chance(self, symbol: str) -> dict[str, Any]:
        status, data, _ = await self.http.request("GET", "/v1/orders/chance", params={"market": symbol}, auth=True)
        if status != 200:
            raise _err(status, data)
        return data

    async def orderable_cash(self, instrument: Instrument, price: Decimal) -> Decimal:
        data = await self._chance(instrument.symbol)
        return D((data.get("bid_account") or {}).get("balance"))

    async def instrument_meta(self, instruments: list[Instrument]) -> list[Instrument]:
        out = []
        for inst in instruments:
            data = await self._chance(inst.symbol)
            mk = data.get("market", {})
            min_total = D((mk.get("bid") or {}).get("min_total") or inst.min_notional)
            max_total = D(mk.get("max_total")) if mk.get("max_total") else None
            state = mk.get("state", "active")
            meta = dict(inst.meta)
            meta.update({"bid_fee": data.get("bid_fee"), "ask_fee": data.get("ask_fee"), "chance_state": state})
            out.append(
                dataclasses.replace(
                    inst, min_notional=min_total, max_notional=max_total,
                    status=inst.status if state == "active" else f"chance:{state}",
                    source=inst.source + " + /v1/orders/chance", meta=meta,
                )
            )
        return out

    @staticmethod
    def _order_body(req: OrderRequest, identifier: str) -> dict[str, Any]:
        if req.order_type != "limit":
            raise BrokerError("unsupported", "이 서비스는 지정가 주문만 사용합니다")
        return {
            "market": req.instrument.symbol,
            "side": "bid" if req.side == Side.BUY else "ask",
            "volume": dstr(req.qty),
            "price": dstr(req.limit_price),
            "ord_type": "limit",
            "identifier": identifier,
        }

    async def _post_order(self, path: str, body: dict[str, Any], group: str) -> SubmitResult:
        try:
            status, data, _ = await self.http.request("POST", path, body=body, auth=True, group=group)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # 연결 수립 전 실패 → 요청이 전송되지 않음(확정)
            return SubmitResult.rejected("not_sent", f"연결 실패: {exc.__class__.__name__}")
        except httpx.HTTPError as exc:
            # 전송 후 응답 유실 가능 → 실패로 단정하지 않는다
            return SubmitResult.unknown("no_response", f"응답 유실: {exc.__class__.__name__}")
        if status in (200, 201):
            return SubmitResult.accepted(str(data.get("uuid")), state=data.get("state"))
        e = _err(status, data)
        if e.kind == "duplicate":
            return SubmitResult.unknown("duplicated_identifier", "이미 등록된 identifier → 기존 주문 조회 필요")
        if status in (429, 418):
            return SubmitResult.rejected(e.code or str(status), e.message)
        if status in _DEFINITE_REJECT_STATUS:
            return SubmitResult.rejected(e.code or str(status), e.message)
        return SubmitResult.unknown(e.code or str(status), e.message)

    async def submit_order(self, req: OrderRequest) -> SubmitResult:
        return await self._post_order("/v1/orders", self._order_body(req, req.client_order_id), "order")

    async def test_order(self, req: OrderRequest) -> SubmitResult:
        # 실제 주문 identifier를 소모하지 않도록 별도 식별자를 사용
        return await self._post_order("/v1/orders/test", self._order_body(req, "t" + req.client_order_id[:30]), "order-test")

    async def get_order(
        self, *, broker_order_id: str | None, client_order_id: str, hint: OrderLookupHint | None = None
    ) -> BrokerOrderState | None:
        params = {"uuid": broker_order_id} if broker_order_id else {"identifier": client_order_id}
        try:
            status, data, _ = await self.http.request("GET", "/v1/order", params=params, auth=True)
        except httpx.HTTPError as exc:
            raise BrokerError("network", f"주문 조회 실패: {exc.__class__.__name__}") from exc
        if status == 200:
            return _order_state(data)
        e = _err(status, data)
        if e.kind == "not_found":
            return None
        raise e

    async def cancel_order(self, *, broker_order_id: str | None, client_order_id: str, instrument: Instrument) -> CancelResult:
        params = {"uuid": broker_order_id} if broker_order_id else {"identifier": client_order_id}
        try:
            status, data, _ = await self.http.request("DELETE", "/v1/order", params=params, auth=True)
        except httpx.HTTPError as exc:
            return CancelResult(accepted=False, error_code="network", error_message=exc.__class__.__name__)
        if status == 200:
            return CancelResult(accepted=True, state=_order_state(data))
        e = _err(status, data)
        return CancelResult(accepted=False, error_code=e.code, error_message=e.message)

    async def open_orders(self, instruments: list[Instrument]) -> list[BrokerOrderState]:
        out: list[BrokerOrderState] = []
        for inst in instruments:
            params = {"market": inst.symbol, "states[]": ["wait", "watch"], "limit": 100}
            status, data, _ = await self.http.request("GET", "/v1/orders/open", params=params, auth=True)
            if status != 200:
                raise _err(status, data)
            out.extend(_order_state(o) for o in data)
        return out

    async def close(self) -> None:
        await self.http.close()
