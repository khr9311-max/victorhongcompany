"""시장별 호가 단위·수량 단위·통화 규칙.

- 업비트 원화마켓: docs.upbit.com/kr/docs/krw-market-info (2026-05-04 갱신본, 2026-09-29 확인).
  실제 주문 전에는 /v1/orderbook/instruments 의 tick_size를 함께 확인하고 더 큰 단위를 사용한다.
- KRX 주식: 2023-01-25 시행 호가가격단위(유가·코스닥 통일). KIS 현재가 응답의 aspr_unit이 있으면 우선한다.
- 미국 주식: 1달러 이상 0.01달러, 1달러 미만 0.0001달러. 소수점(프랙셔널) 주문은 가정하지 않는다.
"""

from __future__ import annotations

from decimal import Decimal

from aifund.core.money import D

_UPBIT_KRW_TABLE: list[tuple[Decimal, Decimal]] = [
    (D("2000000"), D("1000")),
    (D("1000000"), D("1000")),
    (D("500000"), D("500")),
    (D("100000"), D("100")),
    (D("50000"), D("50")),
    (D("10000"), D("10")),
    (D("5000"), D("5")),
    (D("1000"), D("1")),
    (D("100"), D("1")),
    (D("10"), D("0.1")),
    (D("1"), D("0.01")),
    (D("0.1"), D("0.001")),
    (D("0.01"), D("0.0001")),
    (D("0.001"), D("0.00001")),
    (D("0.0001"), D("0.000001")),
    (D("0.00001"), D("0.0000001")),
]

_KRX_TABLE: list[tuple[Decimal, Decimal]] = [
    (D("500000"), D("1000")),
    (D("200000"), D("500")),
    (D("50000"), D("100")),
    (D("20000"), D("50")),
    (D("5000"), D("10")),
    (D("2000"), D("5")),
]

UPBIT_KRW_MIN_ORDER = D("5000")  # 원화마켓 최소 주문 가능 금액(문서 기준)
UPBIT_VOLUME_STEP = D("0.00000001")


def upbit_krw_tick(price: Decimal) -> Decimal:
    for floor, tick in _UPBIT_KRW_TABLE:
        if price >= floor:
            return tick
    return D("0.00000001")


def krx_tick(price: Decimal) -> Decimal:
    for floor, tick in _KRX_TABLE:
        if price >= floor:
            return tick
    return D("1")


def us_tick(price: Decimal) -> Decimal:
    return D("0.01") if price >= 1 else D("0.0001")


def tick_size(policy: str, price: Decimal, fixed: Decimal | None = None) -> Decimal:
    if policy == "upbit_krw":
        base = upbit_krw_tick(price)
        return max(base, fixed) if fixed else base
    if policy == "krx":
        base = krx_tick(price)
        return max(base, fixed) if fixed else base
    if policy == "us":
        return us_tick(price)
    if policy == "fixed" and fixed:
        return fixed
    raise ValueError(f"알 수 없는 호가 정책: {policy}")


def market_currency(market: str) -> str:
    return "USD" if market == "us_stock" else "KRW"


# 미국 거래소 코드: 주문 API와 시세 API가 다른 코드를 쓴다(KIS 공식 예제 기준).
US_ORDER_TO_QUOTE_EXCD = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
