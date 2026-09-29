"""지표 계산(코드가 계산하며 AI가 틱마다 재계산하지 않는다). 신호용 지표는 float, 돈·수량은 Decimal."""

from __future__ import annotations

import math
from collections.abc import Sequence


def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def stdev(values: Sequence[float], period: int) -> float | None:
    if len(values) < period or period < 2:
        return None
    w = values[-period:]
    m = sum(w) / period
    return math.sqrt(sum((x - m) ** 2 for x in w) / period)


def bollinger(values: Sequence[float], period: int, k: float) -> tuple[float, float, float] | None:
    m = sma(values, period)
    s = stdev(values, period)
    if m is None or s is None:
        return None
    return m - k * s, m, m + k * s


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder RSI."""
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def pct_return(values: Sequence[float], bars: int) -> float | None:
    if len(values) <= bars or values[-bars - 1] == 0:
        return None
    return (values[-1] / values[-bars - 1] - 1) * 100


def realized_vol(values: Sequence[float], bars: int) -> float | None:
    if len(values) <= bars:
        return None
    rets = [math.log(values[i] / values[i - 1]) for i in range(len(values) - bars, len(values)) if values[i - 1] > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * 100
