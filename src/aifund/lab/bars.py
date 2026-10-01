"""연구소용 가격 시계열과 공용 지표.

신호 판단 값은 float로 계산한다(strategies/indicators.py와 같은 원칙). 연구소는 원장 금액을 다루지 않고
수익률만 계산하므로 Decimal을 쓰지 않는다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from aifund.domain.models import Candle

REGIMES = ("up", "range", "down")


def bars_per_year(market: str, interval: str) -> float:
    """1년 봉 수. 기간을 '년' 단위로 정한 규칙(52주 신고가 등)을 봉 수로 바꿀 때 쓴다."""
    if interval == "1w":
        return 52.0
    if interval == "1d":
        return 365.0 if market == "crypto" else 252.0
    return 365.0 * 1440 / int(interval[:-1])


@dataclass
class Bars:
    instrument_id: str
    interval: str
    open_time: list[datetime]
    close_time: list[datetime]
    o: list[float]
    h: list[float]
    l: list[float]  # noqa: E741  (OHLC 관례)
    c: list[float]

    @classmethod
    def from_candles(cls, candles: list[Candle]) -> "Bars":
        cs = sorted(candles, key=lambda x: x.open_time)
        if not cs:
            raise ValueError("캔들이 없습니다")
        return cls(cs[0].instrument_id, cs[0].interval, [k.open_time for k in cs], [k.close_time for k in cs],
                   [float(k.open) for k in cs], [float(k.high) for k in cs], [float(k.low) for k in cs],
                   [float(k.close) for k in cs])

    def __len__(self) -> int:
        return len(self.c)

    @property
    def market(self) -> str:
        return self.instrument_id.partition(":")[0]

    def rng(self, t: int) -> float:
        return self.h[t] - self.l[t]


def weekly(b: Bars) -> Bars:
    """일봉 → 주봉(ISO 주, 시가는 첫 봉·종가는 마지막 봉). 아직 끝나지 않은 마지막 주는 뺀다."""
    groups: list[list[int]] = []
    key = None
    for i, t in enumerate(b.open_time):
        k = t.isocalendar()[:2]
        if k != key:
            groups.append([])
            key = k
        groups[-1].append(i)
    last_weekday = 6 if b.market == "crypto" else 4  # 코인은 일요일, 주식은 금요일이 주의 마지막 봉
    if groups and b.open_time[groups[-1][-1]].weekday() < last_weekday:
        groups.pop()
    return Bars(b.instrument_id, "1w", [b.open_time[g[0]] for g in groups], [b.close_time[g[-1]] for g in groups],
                [b.o[g[0]] for g in groups], [max(b.h[i] for i in g) for g in groups],
                [min(b.l[i] for i in g) for g in groups], [b.c[g[-1]] for g in groups])


def sma_series(c: list[float], n: int) -> list[float | None]:
    """단순이동평균. 앞의 n-1개 봉은 None."""
    out: list[float | None] = [None] * len(c)
    s = 0.0
    for i, v in enumerate(c):
        s += v
        if i >= n:
            s -= c[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def prior_max(c: list[float], n: int) -> list[float | None]:
    """out[t] = max(c[t-n:t]) — 봉 t를 뺀 직전 n봉 최고값(신고가 판정용). 앞의 n개 봉은 None."""
    out: list[float | None] = [None] * len(c)
    q: deque[int] = deque()
    for t in range(len(c)):
        if t >= n:
            while q and q[0] < t - n:
                q.popleft()
            out[t] = c[q[0]]
        while q and c[q[-1]] <= c[t]:
            q.pop()
        q.append(t)
    return out


def atr(b: Bars, period: int = 14) -> list[float | None]:
    """Wilder ATR(평균 실제 범위). 앞의 period개 봉은 None."""
    out: list[float | None] = [None] * len(b)
    if len(b) <= period:
        return out
    tr = [b.h[0] - b.l[0]] + [max(b.h[i] - b.l[i], abs(b.h[i] - b.c[i - 1]), abs(b.l[i] - b.c[i - 1]))
                              for i in range(1, len(b))]
    a = sum(tr[1:period + 1]) / period
    out[period] = a
    for i in range(period + 1, len(b)):
        a = (a * (period - 1) + tr[i]) / period
        out[i] = a
    return out


def regimes(c: list[float], n: int = 30, threshold: float = 0.3) -> list[str | None]:
    """국면(코드 판정): n봉 효율비 ER = |순변화| / 변화량 합. ER ≥ threshold면 방향대로 상승·하락, 아니면 횡보.

    책의 추세 판단('선차트가 대체로 오르나, 내리나, 오르내리나')을 숫자로 옮긴 것이다.
    무작위 보행의 ER 기댓값은 약 1/√n(n=30이면 0.18)이라 0.3은 '뚜렷한 방향'을 뜻한다.
    """
    out: list[str | None] = [None] * len(c)
    if len(c) <= n:
        return out
    path = [abs(c[i] - c[i - 1]) for i in range(1, len(c))]  # path[i-1] = |c[i] - c[i-1]|
    s = sum(path[:n])
    for t in range(n, len(c)):
        if t > n:
            s += path[t - 1] - path[t - n - 1]
        net = c[t] - c[t - n]
        er = abs(net) / s if s > 0 else 0.0
        out[t] = "range" if er < threshold else ("up" if net > 0 else "down")
    return out
