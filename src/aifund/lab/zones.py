"""지지·저항 구간(zone) 탐지.

책의 정의를 코드로 옮긴 것:
- 구간은 한 가격이 아니라 범위다 → 피벗 가격들을 ATR 기준 거리로 묶고 앞뒤 여유를 둔다.
- 종가 선차트에서 여러 번 꺾인 곳이 구간이다 → 종가 피벗(좌우 k봉 중 최고·최저)만 쓴다.
- 가격이 반복해서 돌아선 곳 → 피벗 2개 이상. 단, 범위 안 최고·최저 극단은 1개여도 구간으로 둔다.
- 작은 구간은 무시 → k봉 피벗만 쓰고 가까운 피벗은 하나로 합친다.

미래정보 방지: i번째 봉의 피벗은 i+k번째 봉이 끝나야 확정되므로, 봉 t의 구간은 i+k ≤ t인 피벗만 쓴다.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from aifund.lab.bars import Bars


@dataclass(frozen=True)
class Pivot:
    i: int
    price: float
    high: bool


@dataclass(frozen=True)
class Zone:
    center: float
    lo: float  # 구간 하단(여유 포함)
    hi: float  # 구간 상단(여유 포함)
    touches: int
    first: int
    last: int
    extreme: bool  # 범위 안 최고·최저 무리


def close_pivots(c: list[float], k: int) -> list[Pivot]:
    """종가 피벗. 같은 값이 이어지면 첫 봉 하나만 피벗으로 센다."""
    out: list[Pivot] = []
    for i in range(k, len(c) - k):
        left, right = c[i - k:i], c[i + 1:i + k + 1]
        if c[i] > max(left) and c[i] >= max(right):
            out.append(Pivot(i, c[i], True))
        elif c[i] < min(left) and c[i] <= min(right):
            out.append(Pivot(i, c[i], False))
    return out


def cluster(pivots: list[Pivot], tol: float, min_touches: int) -> list[Zone]:
    """가격순으로 정렬해 무리 평균에서 tol 이내면 같은 구간으로 묶는다. 결과는 가격 오름차순."""
    if not pivots or tol <= 0:
        return []
    srt = sorted(pivots, key=lambda p: p.price)
    groups: list[list[Pivot]] = [[srt[0]]]
    for p in srt[1:]:
        g = groups[-1]
        if p.price - sum(x.price for x in g) / len(g) <= tol:
            g.append(p)
        else:
            groups.append([p])
    zones = []
    for n, g in enumerate(groups):
        extreme = n in (0, len(groups) - 1)
        if len(g) < min_touches and not extreme:
            continue
        prices = [x.price for x in g]
        zones.append(Zone(sum(prices) / len(prices), min(prices) - tol / 2, max(prices) + tol / 2, len(g),
                          min(x.i for x in g), max(x.i for x in g), extreme))
    return zones


class ZoneMap:
    """봉마다 그 시점에 확정된 정보만으로 구간을 계산한다(같은 봉은 한 번만 계산)."""

    def __init__(self, bars: Bars, atr: list[float | None], *, pivot_k: int = 5, lookback: int = 400,
                 width_atr: float = 0.5, min_touches: int = 2) -> None:
        self.atr = atr
        self.k = pivot_k
        self.lookback = lookback
        self.width_atr = width_atr
        self.min_touches = min_touches
        self.pivots = close_pivots(bars.c, pivot_k)
        self._idx = [p.i for p in self.pivots]
        self._cache: dict[int, list[Zone]] = {}

    def tol(self, t: int) -> float | None:
        a = self.atr[t]
        return None if a is None or a <= 0 else self.width_atr * a

    def pivots_between(self, lo_i: int, hi_i: int, t: int) -> list[Pivot]:
        """봉 t 시점에 확정된 피벗 중 번호가 lo_i~hi_i인 것."""
        hi_i = min(hi_i, t - self.k)
        if hi_i < lo_i:
            return []
        return self.pivots[bisect_left(self._idx, lo_i):bisect_right(self._idx, hi_i)]

    def at(self, t: int) -> list[Zone]:
        """봉 t 종가 시점의 구간(가격 오름차순)."""
        cached = self._cache.get(t)
        if cached is not None:
            return cached
        tol = self.tol(t)
        zones = [] if tol is None else cluster(self.pivots_between(t - self.lookback, t, t), tol, self.min_touches)
        self._cache[t] = zones
        return zones
