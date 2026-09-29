"""금액·수량 계산. 실제 돈과 수량은 반드시 Decimal로 다룬다."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation, getcontext
from typing import Any

getcontext().prec = 34

ZERO = Decimal(0)
ONE = Decimal(1)


def D(value: Any) -> Decimal:
    """안전한 Decimal 변환. float는 문자열을 거쳐 이진 오차를 줄인다."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool은 금액이 아닙니다")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - 방어
        raise ValueError(f"숫자로 변환할 수 없습니다: {value!r}") from exc


def dstr(value: Decimal | None) -> str | None:
    """DB 저장용 고정소수 문자열(지수 표기 없음)."""
    if value is None:
        return None
    v = D(value)
    if v == v.to_integral_value():
        return format(v.quantize(ONE), "f")
    return format(v.normalize(), "f")


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step은 0보다 커야 합니다")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step은 0보다 커야 합니다")
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def is_multiple(value: Decimal, step: Decimal) -> bool:
    return (value / step) == (value / step).to_integral_value()


def round_krw(value: Decimal) -> Decimal:
    return value.quantize(ONE, rounding=ROUND_HALF_UP)


def fmt_krw(value: Decimal | None, sign: bool = False) -> str:
    if value is None:
        return "-"
    v = round_krw(D(value))
    s = f"{v:,.0f}"
    if sign and v > 0:
        s = "+" + s
    return s + "원"


def fmt_num(value: Decimal | None, places: int = 8) -> str:
    if value is None:
        return "-"
    v = D(value)
    q = Decimal(1).scaleb(-places)
    return f"{v.quantize(q, rounding=ROUND_HALF_UP).normalize():,f}"


def pct(numer: Decimal, denom: Decimal) -> Decimal | None:
    if denom == 0:
        return None
    return (numer / denom) * 100
