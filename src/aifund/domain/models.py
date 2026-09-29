"""도메인 모델."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from aifund.core.money import ZERO


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED})
OPEN_STATUSES = frozenset(
    {OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCEL_PENDING, OrderStatus.UNKNOWN}
)

STATUS_LABELS = {
    "pending": "전송 대기",
    "submitted": "접수",
    "partially_filled": "부분체결",
    "filled": "체결 완료",
    "cancel_pending": "취소 요청됨",
    "canceled": "취소 완료",
    "rejected": "거부",
    "unknown": "상태 불명(조회 중)",
}


class Action(StrEnum):
    BUY = "buy"
    HOLD = "hold"
    REDUCE = "reduce"
    SELL = "sell"
    WAIT = "wait"


ACTION_LABELS = {"buy": "매수", "hold": "보유", "reduce": "축소", "sell": "매도", "wait": "관망"}


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    market: str
    symbol: str
    exchange: str | None
    name: str | None
    quote_ccy: str
    base_asset: str
    tick_policy: str  # upbit_krw / krx / us / fixed
    fixed_tick: Decimal | None
    qty_step: Decimal
    min_notional: Decimal
    max_notional: Decimal | None
    status: str = "active"
    source: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def tick_for(self, price: Decimal) -> Decimal:
        from aifund.markets.rules import tick_size

        return tick_size(self.tick_policy, price, self.fixed_tick)


@dataclass(frozen=True)
class Candle:
    instrument_id: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal | None = None
    source: str = ""


@dataclass(frozen=True)
class Quote:
    instrument_id: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    last: Decimal | None
    turnover_24h: Decimal | None
    ts_exchange: datetime | None
    fetched_at: datetime
    source: str
    quote_id: int | None = None

    @property
    def mid(self) -> Decimal | None:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread_pct(self) -> Decimal | None:
        if self.bid and self.ask and self.bid > 0:
            return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 100
        return None


@dataclass(frozen=True)
class FxRate:
    pair: str
    rate: Decimal
    as_of: datetime
    fetched_at: datetime
    source: str
    source_url: str | None = None


@dataclass(frozen=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    instrument: Instrument
    side: Side
    qty: Decimal
    limit_price: Decimal
    order_type: str = "limit"


@dataclass(frozen=True)
class TradeFill:
    trade_id: str
    qty: Decimal
    price: Decimal
    fee: Decimal | None
    ts: datetime | None


@dataclass
class BrokerOrderState:
    broker_order_id: str | None
    client_order_id: str | None
    status: OrderStatus
    filled_qty: Decimal
    filled_amount: Decimal | None  # 누적 체결금액(알 수 있으면)
    fee_total: Decimal | None
    trades: list[TradeFill] | None  # 개별 체결 목록(거래소가 제공하는 경우)
    raw_state: str
    instrument_id: str | None = None
    side: Side | None = None
    qty: Decimal | None = None
    limit_price: Decimal | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubmitResult:
    outcome: str  # accepted / rejected / unknown
    broker_order_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def accepted(broker_order_id: str, **meta: Any) -> "SubmitResult":
        return SubmitResult("accepted", broker_order_id=broker_order_id, meta=meta)

    @staticmethod
    def rejected(code: str, message: str) -> "SubmitResult":
        return SubmitResult("rejected", error_code=code, error_message=message)

    @staticmethod
    def unknown(code: str, message: str) -> "SubmitResult":
        return SubmitResult("unknown", error_code=code, error_message=message)


@dataclass(frozen=True)
class CancelResult:
    accepted: bool
    already_final: bool = False
    error_code: str | None = None
    error_message: str | None = None
    state: BrokerOrderState | None = None


@dataclass(frozen=True)
class AccountCheck:
    ok: bool
    auth_ok: bool
    detail: dict[str, Any]
    clock_skew_sec: float | None = None
    error: str | None = None
