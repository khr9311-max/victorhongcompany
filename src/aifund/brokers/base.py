"""BrokerAdapter 인터페이스와 공통 오류.

어댑터는 거래소별 차이(클라이언트 주문 ID 지원 여부, 체결 상세 제공 여부 등)를 흡수하고
capabilities로 지원/미지원 기능을 명시한다. 문서로 확인되지 않은 기능은 False로 둔다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from aifund.domain.models import (
    AccountCheck,
    Balance,
    BrokerOrderState,
    CancelResult,
    Candle,
    Instrument,
    OrderRequest,
    Quote,
    SubmitResult,
)


class BrokerError(Exception):
    """거래소 호출 오류.

    kind: auth / permission / rate_limited / blocked / invalid / insufficient / not_found /
          market_offline / server / network / unsupported / duplicate / unknown
    """

    def __init__(self, kind: str, message: str, *, code: str | None = None, http_status: int | None = None) -> None:
        super().__init__(f"[{kind}{'/' + code if code else ''}] {message}")
        self.kind = kind
        self.code = code
        self.http_status = http_status
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.kind in {"rate_limited", "server", "network"}


@dataclass(frozen=True)
class BrokerCapabilities:
    quotes: bool = False
    candles: bool = False
    instrument_meta: bool = False
    balances: bool = False
    orderable_cash: bool = False
    place_order: bool = False
    order_test: bool = False
    cancel_order: bool = False
    client_order_id: bool = False  # 클라이언트 주문 ID로 조회 가능한가
    open_orders: bool = False
    per_trade_fills: bool = False  # 개별 체결 목록 제공
    exchange_fees_reported: bool = False
    server_side_stop: bool = False  # 거래소 보호(손절) 주문. 확인된 것만 True
    sandbox: bool = False
    notes: tuple[str, ...] = ()

    def as_rows(self) -> list[tuple[str, bool]]:
        labels = {
            "quotes": "시세 조회",
            "candles": "캔들 조회",
            "instrument_meta": "상품 메타데이터",
            "balances": "잔고 조회",
            "orderable_cash": "주문 가능 금액",
            "place_order": "주문 생성",
            "order_test": "주문 검증(미체결 테스트)",
            "cancel_order": "주문 취소",
            "client_order_id": "클라이언트 주문 ID 조회",
            "open_orders": "미체결 조회",
            "per_trade_fills": "개별 체결 내역",
            "exchange_fees_reported": "거래소 수수료 보고",
            "server_side_stop": "거래소 측 손절 주문",
            "sandbox": "공식 모의투자",
        }
        d = asdict(self)
        return [(labels[k], bool(d[k])) for k in labels]


@dataclass(frozen=True)
class OrderLookupHint:
    """클라이언트 주문 ID가 없는 거래소(KIS)에서 응답 유실 주문을 찾기 위한 단서."""

    instrument: Instrument
    side: str
    qty: Decimal
    limit_price: Decimal
    submitted_after: datetime
    exclude_broker_ids: frozenset[str] = frozenset()


class MarketData(ABC):
    """시세 데이터 원천(공개 시세, KIS 시세, 데모, 재생)."""

    source_name: str = "unknown"
    is_demo: bool = False

    @abstractmethod
    async def instruments(self, market: str, symbols: list[str]) -> list[Instrument]: ...

    @abstractmethod
    async def candles(self, instrument: Instrument, interval: str, count: int) -> list[Candle]:
        """최신순이 아닌 시간 오름차순. 미완성 봉 포함 여부는 호출자가 close_time으로 판단."""

    @abstractmethod
    async def quotes(self, instruments: list[Instrument]) -> list[Quote]: ...

    async def close(self) -> None:  # pragma: no cover - 기본 구현
        return None


class BrokerAdapter(ABC):
    name: str = "base"
    is_live_money: bool = False

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities: ...

    @abstractmethod
    async def check_account(self) -> AccountCheck: ...

    @abstractmethod
    async def balances(self) -> dict[str, Balance]: ...

    @abstractmethod
    async def orderable_cash(self, instrument: Instrument, price: Decimal) -> Decimal: ...

    @abstractmethod
    async def submit_order(self, req: OrderRequest) -> SubmitResult: ...

    async def test_order(self, req: OrderRequest) -> SubmitResult:
        return SubmitResult.rejected("unsupported", "이 거래소는 주문 검증 API를 제공하지 않습니다")

    @abstractmethod
    async def get_order(
        self, *, broker_order_id: str | None, client_order_id: str, hint: OrderLookupHint | None = None
    ) -> BrokerOrderState | None:
        """없으면 None(확정적으로 존재하지 않음). 조회 자체 실패는 BrokerError."""

    @abstractmethod
    async def cancel_order(self, *, broker_order_id: str | None, client_order_id: str, instrument: Instrument) -> CancelResult: ...

    @abstractmethod
    async def open_orders(self, instruments: list[Instrument]) -> list[BrokerOrderState]: ...

    async def instrument_meta(self, instruments: list[Instrument]) -> list[Instrument]:
        return instruments

    async def close(self) -> None:
        return None
