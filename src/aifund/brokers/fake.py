"""스크립트형 가짜 거래소(테스트·실행무결성 자체검증 전용). 실제 네트워크를 쓰지 않는다.

재현 가능한 장애:
- lose_next_response: 주문은 거래소에 생성되지만 응답이 유실된다(unknown 경로 검증).
- reject_next: 확정 거부.
- 체결 스크립트: 주문별로 조회 때마다 공개할 체결 목록(부분체결·중복·순서 역전 포함).
- 취소: 요청 수락 후 다음 조회에서야 cancel 확정(취소 중 추가 체결 가능).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from aifund.brokers.base import BrokerAdapter, BrokerCapabilities, OrderLookupHint
from aifund.core.money import ZERO
from aifund.domain.models import (
    AccountCheck,
    Balance,
    BrokerOrderState,
    CancelResult,
    Instrument,
    OrderRequest,
    OrderStatus,
    Side,
    SubmitResult,
    TradeFill,
)


@dataclass
class FakeOrder:
    broker_order_id: str
    client_order_id: str
    instrument: Instrument
    side: Side
    qty: Decimal
    price: Decimal
    state: str = "wait"  # wait / done / cancel
    revealed: list[TradeFill] = field(default_factory=list)
    script: list[list[TradeFill]] = field(default_factory=list)  # 조회마다 공개할 체결(누적 목록 아님: 이번에 새로 보이는 것)
    cancel_requested: bool = False
    cumulative_only: bool = False


class FakeBroker(BrokerAdapter):
    name = "fake"

    def __init__(self, account_id: str = "fake-acct", *, client_ids: bool = True, live_money: bool = False,
                 cash: Decimal = Decimal("1000000")) -> None:
        super().__init__(account_id)
        self.orders: dict[str, FakeOrder] = {}
        self.submit_calls = 0
        self.cancel_calls = 0
        self.lose_next_response = False
        self.reject_next: str | None = None
        self.client_ids = client_ids
        self.is_live_money = live_money
        self.cash = cash
        self.holdings: dict[str, Decimal] = {}
        self.default_script: list[list[TradeFill]] | None = None
        self.cumulative_only = False
        self.foreign_open: list[BrokerOrderState] = []
        self._n = 0

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(balances=True, orderable_cash=True, place_order=True, order_test=True, cancel_order=True,
                                  client_order_id=self.client_ids, open_orders=True, per_trade_fills=not self.cumulative_only,
                                  exchange_fees_reported=True)

    async def check_account(self) -> AccountCheck:
        return AccountCheck(ok=True, auth_ok=True, detail={"fake": True}, clock_skew_sec=0.1)

    async def balances(self) -> dict[str, Balance]:
        out = {"KRW": Balance("KRW", self.cash)}
        for k, v in self.holdings.items():
            out[k] = Balance(k, v)
        return out

    async def orderable_cash(self, instrument: Instrument, price: Decimal) -> Decimal:
        return self.cash

    async def submit_order(self, req: OrderRequest) -> SubmitResult:
        self.submit_calls += 1
        if self.reject_next:
            code, self.reject_next = self.reject_next, None
            return SubmitResult.rejected(code, "스크립트 거부")
        if any(o.client_order_id == req.client_order_id for o in self.orders.values()):
            return SubmitResult.unknown("duplicated_identifier", "중복 identifier")
        self._n += 1
        boid = f"fake-{self._n}"
        o = FakeOrder(boid, req.client_order_id, req.instrument, req.side, req.qty, req.limit_price,
                      script=[list(x) for x in (self.default_script or [])], cumulative_only=self.cumulative_only)
        self.orders[boid] = o
        if self.lose_next_response:
            self.lose_next_response = False
            return SubmitResult.unknown("no_response", "스크립트: 응답 유실(주문은 생성됨)")
        return SubmitResult.accepted(boid)

    async def test_order(self, req: OrderRequest) -> SubmitResult:
        if req.qty <= 0 or req.qty * req.limit_price < req.instrument.min_notional:
            return SubmitResult.rejected("under_min_total", "최소 주문 금액 미만")
        return SubmitResult.accepted("fake-test")

    def _find(self, broker_order_id: str | None, client_order_id: str, hint: OrderLookupHint | None) -> FakeOrder | None:
        if broker_order_id and broker_order_id in self.orders:
            return self.orders[broker_order_id]
        if self.client_ids:
            return next((o for o in self.orders.values() if o.client_order_id == client_order_id), None)
        if hint is not None:
            c = [o for o in self.orders.values() if o.qty == hint.qty and o.price == hint.limit_price and o.side.value == hint.side
                 and o.broker_order_id not in hint.exclude_broker_ids]
            return c[0] if len(c) == 1 else None
        return None

    def _state(self, o: FakeOrder) -> BrokerOrderState:
        filled = sum((t.qty for t in o.revealed), ZERO)
        amount = sum((t.qty * t.price for t in o.revealed), ZERO)
        if o.state == "done":
            st = OrderStatus.FILLED
        elif o.state == "cancel":
            st = OrderStatus.CANCELED
        else:
            st = OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.SUBMITTED
        return BrokerOrderState(o.broker_order_id, o.client_order_id if self.client_ids else None, st, filled, amount,
                                sum((t.fee or ZERO for t in o.revealed), ZERO),
                                None if o.cumulative_only else list(o.revealed), o.state, o.instrument.instrument_id, o.side, o.qty, o.price)

    def advance(self, o: FakeOrder) -> None:
        if o.state != "wait":
            return
        if o.script:
            step = o.script.pop(0)
            for t in step:
                # 같은 trade_id가 다시 오면(중복 이벤트) 거래소 측 누적에는 한 번만 반영
                if all(x.trade_id != t.trade_id for x in o.revealed):
                    o.revealed.append(t)
        filled = sum((t.qty for t in o.revealed), ZERO)
        if filled >= o.qty:
            o.state = "done"
        elif o.cancel_requested and not o.script:
            o.state = "cancel"

    async def get_order(self, *, broker_order_id: str | None, client_order_id: str,
                        hint: OrderLookupHint | None = None) -> BrokerOrderState | None:
        o = self._find(broker_order_id, client_order_id, hint)
        if o is None:
            return None
        self.advance(o)
        return self._state(o)

    async def cancel_order(self, *, broker_order_id: str | None, client_order_id: str, instrument: Instrument) -> CancelResult:
        self.cancel_calls += 1
        o = self._find(broker_order_id, client_order_id, None)
        if o is None:
            return CancelResult(False, error_code="order_not_found", error_message="없음")
        if o.state != "wait":
            return CancelResult(False, already_final=True, state=self._state(o))
        o.cancel_requested = True
        return CancelResult(True, state=self._state(o))

    async def open_orders(self, instruments: list[Instrument]) -> list[BrokerOrderState]:
        return [self._state(o) for o in self.orders.values() if o.state == "wait"] + list(self.foreign_open)
