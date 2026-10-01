"""진입 규칙. 계열 둘:

- 책 패턴(pattern): Naked Forex(Nekritin·Peters, 2012) 5~10장의 촉매를 매수 전용 규칙으로 옮긴 것.
  (1) 지지·저항 구간 (2) 가격이 구간에 도달 (3) 구간에서 촉매 캔들 → 패턴 봉 고가 위로 올라가면 진입(책: 역지정가 매수).
  손절은 패턴 너머, 목표는 위쪽 구간. 이 시스템은 현물 매수만 하므로 하락 패턴(물라 등)은 쓰지 않는다.
  책이 '최적 조건'으로 반복 강조한 것(왼쪽 공간 7봉 등)은 정의에 넣었다.
- 추세(trend): 종목별 타이밍. 52주 신고가 돌파·200일선 상향 돌파 → 다음 봉 시가 진입, 초기 손절은 3 ATR 아래.
  기간은 '년' 단위로 정해 일봉·주봉에서 같은 뜻이 되게 한다(52주 = 일봉 252봉 = 주봉 52봉).

숫자가 없는 표현('근처', '작은 쉼')과 교과서 기간값은 Rules·TrendRules에 고정했다. 결과를 보고 값을 고치면 그만큼
시도 횟수가 늘어난 것이다(과최적화). 고칠 때는 RULES_VERSION을 올리고 보고서에 남긴다.
판단은 봉 t까지의 정보만 쓴다(구간도 t 시점에 확정된 피벗만). tests/test_lab.py가 잘라낸 데이터로 확인한다.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from aifund.lab.bars import Bars, atr, bars_per_year, prior_max, regimes, sma_series
from aifund.lab.zones import Zone, ZoneMap

RULES_VERSION = "3"  # 1: 책 패턴 / 2: 추세·순환 계열 추가 / 3: 책 패턴 해석 넷(사전 등록, v1 규칙은 그대로)
RANDOM_RATE = 0.02  # 대조군: 봉의 2%에서 무작위 진입


@dataclass(frozen=True)
class Rules:
    """책 패턴 규칙. 기본값이 v1 해석이고, 다른 해석은 INTERPRETATIONS가 일부 값만 바꾼다."""

    room_left: int = 7  # 캥거루 꼬리·빅 벨트: 저가가 직전 N봉보다 낮음(0이면 따지지 않음)
    range_lookback: int = 10  # 캥거루 꼬리·빅 벨트 길이 비교 범위
    kt_top: float = 1 / 3  # 캥거루 꼬리: 시가·종가가 봉의 위쪽 이 비율 안
    kt_range: str = "avg"  # 캥거루 꼬리 길이: avg(직전 N봉 평균 이상) / prev(직전 1봉보다 큼) / max(직전 N봉 모두보다 큼)
    kt_inside_prev: bool = True  # 캥거루 꼬리: 시가·종가가 직전 봉 범위 안(배치 규칙)
    kt_bullish: bool = False  # 캥거루 꼬리: 종가 > 시가
    kt_cut75: bool = False  # 캥거루 꼬리: 종가가 손절까지 거리의 75% 이상 내려가면 청산
    shadow_lookback: int = 5  # 빅 섀도: 직전 N봉 중 가장 큰 범위
    shadow_extreme: int = 7  # 빅 섀도: 저가가 직전 N봉보다 낮음(극단)
    close_near: float = 0.25  # 빅 섀도: 종가가 범위의 위쪽 이 비율 안
    tail_third: float = 1 / 3  # 트렌디 캥거루: 시가·종가가 봉의 위쪽 1/3 안
    belt_near: float = 0.2  # 빅 벨트: 시가는 저가 근처, 종가는 고가 근처(범위의 이 비율 안)
    belt_gap_atr: float = 0.1  # 빅 벨트: 직전 종가보다 ATR의 이 배수 이상 낮게 시작(0이면 낮기만 하면 됨)
    belt_range: bool = True  # 빅 벨트: 범위 ≥ 직전 N봉 평균
    belt_week_start: bool = False  # 빅 벨트: 그 주 첫 거래일만
    wammie_gap: int = 6  # 와미: 두 터치 사이 최소 봉 수
    wammie_window: int = 60  # 와미: 첫 터치를 찾는 범위(봉)
    wammie_rally_atr: float = 1.0  # 와미: 두 터치 사이 구간 위로 벗어난 폭
    wammie_strong: bool = True  # 와미: 두 번째 터치 봉이 위쪽 절반에서 마감한 양봉(아니면 양봉만)
    wammie_catalyst: bool = False  # 와미: 두 번째 터치 봉이 캥거루 꼬리·빅 섀도 모양
    box_window: int = 60  # 라스트 키스: 박스를 찾는 범위(봉)
    box_min_bars: int = 10  # 박스 최소 길이
    box_min_atr: float = 1.0  # 박스 높이 하한·상한(ATR 배수)
    box_max_atr: float = 8.0
    kiss_within: int = 10  # 돌파 후 이 봉 수 안에 박스 상단으로 돌아와야 '키스'
    kiss_strong: bool = True  # 라스트 키스: 키스 봉이 위쪽 절반에서 마감한 양봉(아니면 양봉만)
    pause_min: int = 3  # 트렌디 캥거루: 3~10봉 쉼
    pause_max: int = 10
    pause_atr: float = 2.0  # 쉼 구간 폭 상한(ATR 배수)
    correction_atr: float = 3.0  # '큰 조정 뒤가 아님': 쉼 직전 고점 - 쉼 저점 ≤ 3 ATR
    tk_zone: bool = False  # 트렌디 캥거루: 지지 구간 위
    buffer_atr: float = 0.1  # 진입가·손절가 여유('몇 핍')
    entry_window: int = 3  # 패턴 뒤 진입 유효 봉 수
    quick_trigger: bool = False  # 캥거루 꼬리·빅 섀도는 다음 1봉 안에 진입해야 함
    min_rr: float = 1.0  # 구간 청산 목표: 위험의 이 배수 이상 떨어진 첫 구간(0이면 가장 가까운 구간)
    regime_filter: bool = False  # 반전 패턴·무작위 대조군은 횡보 국면에서만


@dataclass(frozen=True)
class Interpretation:
    """책 문구를 숫자로 옮기는 방식 하나. 사전 등록: docs/lab-preregistration.md."""

    key: str
    label: str
    rules: Rules
    pivot_k: int = 5
    zone_lookback: int = 400
    note: str = ""


INTERPRETATIONS: dict[str, Interpretation] = {i.key: i for i in (
    Interpretation("v1", "v1", Rules(), note="지금까지의 해석"),
    Interpretation("loose", "느슨", Rules(
        room_left=0, kt_range="prev", shadow_lookback=1, shadow_extreme=3, close_near=1 / 3, belt_near=1 / 3,
        belt_gap_atr=0.0, belt_range=False, wammie_rally_atr=0.5, wammie_strong=False, kiss_strong=False, min_rr=0.0),
        note="책이 '규칙'이라고 한 것만, '최적 조건'과 덧붙인 숫자는 뺌"),
    Interpretation("strict", "엄격", Rules(
        kt_top=0.25, kt_range="max", kt_bullish=True, kt_cut75=True, shadow_lookback=10, close_near=0.1, belt_near=0.1,
        belt_week_start=True, wammie_gap=20, wammie_catalyst=True, tk_zone=True, quick_trigger=True),
        pivot_k=10, zone_lookback=2000, note="책이 '최적·최상'이라고 한 조건을 모두, 큰 구간만"),
    Interpretation("regime", "국면 맞춤", Rules(regime_filter=True),
                   note="v1 + 반전 패턴·무작위 대조군은 횡보 국면에서만(5장)"),
)}
REVERSAL = frozenset({"kangaroo_tail", "big_shadow", "big_belt", "wammie", "random"})  # 국면 맞춤 대상
QUICK = frozenset({"kangaroo_tail", "big_shadow"})  # 엄격: 다음 1봉 안에 진입


@dataclass(frozen=True)
class TrendRules:
    """추세 계열 기간(교과서 값, 최적화하지 않음). 년 단위."""

    high_years: float = 1.0  # 52주 신고가
    ma_years: float = 200 / 252  # 200일선(주봉 약 40주)
    exit_ma_years: float = 50 / 252  # 50일선(주봉 약 10주)
    stop_atr: float = 3.0  # 초기 손절·ATR 추적 폭


@dataclass(frozen=True)
class Setup:
    kind: str
    t: int  # 패턴 봉(종가에 확정)
    trigger: float  # 이 가격을 넘으면 진입
    stop: float  # 초기 손절
    targets: tuple[float, ...]  # 위쪽 구간 하단(가까운 순). 청산 방식이 사용
    box_top: float | None = None  # 라스트 키스: 종가가 이 아래(박스 안)로 돌아오면 청산
    at_open: bool = False  # 추세 계열: 확인 없이 다음 봉 시가에 진입
    window: int = 3  # 패턴 뒤 진입 유효 봉 수


class Context:
    """한 종목의 패턴 판단 재료(지표·구간·국면)를 한 번만 계산해 모든 패턴·변형이 함께 쓴다."""

    def __init__(self, bars: Bars, rules: Rules | None = None, *, trend: TrendRules | None = None, atr_period: int = 14,
                 regime_n: int = 30, regime_threshold: float = 0.3, pivot_k: int = 5, zone_lookback: int = 400,
                 zone_width_atr: float = 0.5) -> None:
        self.b = bars
        self.r = rules or Rules()
        self.tr = trend or TrendRules()
        self.bpy = bars_per_year(bars.market, bars.interval)
        self.atr = atr(bars, atr_period)
        self.zones = ZoneMap(bars, self.atr, pivot_k=pivot_k, lookback=zone_lookback, width_atr=zone_width_atr)
        self.regime = regimes(bars.c, regime_n, regime_threshold)
        self.ranges = [bars.h[i] - bars.l[i] for i in range(len(bars))]
        r = self.r
        self.warmup = max(atr_period, regime_n, r.range_lookback, r.room_left, r.shadow_lookback, r.shadow_extreme,
                          r.box_min_bars) + 2
        self._boxes: dict[tuple[int, int], tuple[float, float] | None] = {}
        self._sma: dict[int, list[float | None]] = {}
        self._prior_max: dict[int, list[float | None]] = {}

    @classmethod
    def for_interp(cls, bars: Bars, interp: Interpretation) -> "Context":
        return cls(bars, interp.rules, pivot_k=interp.pivot_k, zone_lookback=interp.zone_lookback)

    def window(self, years: float) -> int:
        """년 단위 기간 → 이 봉 간격의 봉 수."""
        return max(2, round(years * self.bpy))

    def sma(self, n: int) -> list[float | None]:
        if n not in self._sma:
            self._sma[n] = sma_series(self.b.c, n)
        return self._sma[n]

    def prior_max(self, n: int) -> list[float | None]:
        if n not in self._prior_max:
            self._prior_max[n] = prior_max(self.b.c, n)
        return self._prior_max[n]

    def buf(self, t: int) -> float:
        return self.r.buffer_atr * (self.atr[t] or 0.0)

    def at_open(self, kind: str, t: int) -> Setup:
        """추세 계열: 다음 봉 시가 진입, 초기 손절은 종가 - 3 ATR."""
        c = self.b.c[t]
        return Setup(kind, t, c, c - self.tr.stop_atr * (self.atr[t] or 0.0), (), at_open=True)

    def avg_range(self, t: int, n: int) -> float:
        return sum(self.ranges[t - n:t]) / n

    def room_left(self, t: int, n: int) -> bool:
        """저가가 직전 n봉의 저가보다 낮다(한동안 거래되지 않은 가격대). n이 0이면 따지지 않는다."""
        return n <= 0 or self.b.l[t] < min(self.b.l[t - n:t])

    def support(self, t: int) -> Zone | None:
        """봉 t의 아래쪽(저가 쪽 절반)이 구간에 닿고 구간 하단 위에서 마감했다면 그 구간(가장 높은 것)."""
        b = self.b
        lower_half = b.l[t] + self.ranges[t] / 2
        for z in reversed(self.zones.at(t)):
            if b.l[t] <= z.hi and z.center <= lower_half and b.c[t] >= z.lo:
                return z
        return None

    def setup(self, kind: str, t: int, stop: float, box_top: float | None = None) -> Setup:
        trigger = self.b.h[t] + self.buf(t)
        targets = tuple(z.lo for z in self.zones.at(t) if z.lo > trigger)
        window = 1 if self.r.quick_trigger and kind in QUICK else self.r.entry_window
        return Setup(kind, t, trigger, stop, targets, box_top, window=window)

    def box_before(self, brk: int, t: int) -> tuple[float, float] | None:
        """brk 직전 박스(하단, 상단): 위아래 각각 2회 이상 터치, 높이 1~8 ATR, 10봉 이상, 그동안 종가가 박스 안.

        봉 t 시점에 확정된 피벗만 쓴다. 같은 피벗 집합이면 결과가 같으므로 기억해 둔다.
        """
        r = self.r
        key = (brk, min(brk - 1, t - self.zones.k))
        if key in self._boxes:
            return self._boxes[key]
        out = None
        tol, a = self.zones.tol(brk), self.atr[brk]
        ps = self.zones.pivots_between(brk - r.box_window, brk - 1, t)
        highs = [p for p in ps if p.high]
        lows = [p for p in ps if not p.high]
        if tol is not None and a and len(highs) >= 2 and len(lows) >= 2:
            top, bottom = max(p.price for p in highs), min(p.price for p in lows)
            touch_hi = [p.i for p in highs if p.price >= top - tol]
            touch_lo = [p.i for p in lows if p.price <= bottom + tol]
            if (r.box_min_atr * a <= top - bottom <= r.box_max_atr * a and len(touch_hi) >= 2 and len(touch_lo) >= 2):
                first = min(touch_hi + touch_lo)
                c = self.b.c
                if brk - first >= r.box_min_bars and all(bottom - tol <= c[i] <= top + tol for i in range(first, brk)):
                    out = (bottom, top)
        self._boxes[key] = out
        return out


# ----------------------------------------------------------------------------- 패턴(매수)


def _kt_shape(x: Context, t: int, top: float = 1 / 3) -> bool:
    """캥거루 꼬리 모양: 시가·종가가 봉의 위쪽 top 비율 안."""
    rng = x.ranges[t]
    return rng > 0 and min(x.b.o[t], x.b.c[t]) >= x.b.l[t] + rng * (1 - top)


def _shadow_shape(x: Context, t: int) -> bool:
    """빅 섀도 모양: 직전 봉을 위아래로 감싼 양봉, 종가가 위쪽 25% 안."""
    b, rng = x.b, x.ranges[t]
    return b.h[t] > b.h[t - 1] and b.l[t] < b.l[t - 1] and b.o[t] < b.c[t] >= b.h[t] - rng * 0.25


def kangaroo_tail(x: Context, t: int) -> Setup | None:
    b, r = x.b, x.r
    rng = x.ranges[t]
    if not _kt_shape(x, t, r.kt_top):
        return None  # 시가·종가가 위쪽 1/3(엄격: 1/4) 밖이면 캥거루 꼬리가 아니다
    n = r.range_lookback
    if ((r.kt_range == "prev" and rng <= x.ranges[t - 1]) or (r.kt_range == "avg" and rng < x.avg_range(t, n))
            or (r.kt_range == "max" and rng <= max(x.ranges[t - n:t]))):
        return None  # 주변 봉보다 짧다
    if r.kt_inside_prev and not (b.l[t - 1] <= b.o[t] <= b.h[t - 1] and b.l[t - 1] <= b.c[t] <= b.h[t - 1]):
        return None  # 배치 규칙: 시가·종가가 직전 봉 범위 안
    if r.kt_bullish and b.c[t] <= b.o[t]:
        return None
    if not x.room_left(t, r.room_left) or x.support(t) is None:
        return None
    return x.setup("kangaroo_tail", t, b.l[t] - x.buf(t))


def big_shadow(x: Context, t: int) -> Setup | None:
    b, r = x.b, x.r
    rng = x.ranges[t]
    if not (b.h[t] > b.h[t - 1] and b.l[t] < b.l[t - 1]) or b.c[t] <= b.o[t]:
        return None  # 직전 봉을 위아래로 감싼 양봉
    if b.c[t] < b.h[t] - rng * r.close_near:
        return None  # 종가가 고가 근처가 아니다
    if rng <= max(x.ranges[t - r.shadow_lookback:t]):
        return None  # 직전 N봉 중 가장 큰 범위가 아니다
    if not x.room_left(t, r.shadow_extreme) or x.support(t) is None:
        return None
    return x.setup("big_shadow", t, b.l[t] - x.buf(t))


def big_belt(x: Context, t: int) -> Setup | None:
    b, r = x.b, x.r
    rng, a = x.ranges[t], x.atr[t] or 0.0
    gap = b.c[t - 1] - b.o[t]
    if rng <= 0 or gap <= 0 or gap < r.belt_gap_atr * a:
        return None  # 직전 종가보다 낮게(갭 하락) 시작해야 한다
    if b.o[t] - b.l[t] > rng * r.belt_near or b.h[t] - b.c[t] > rng * r.belt_near:
        return None  # 시가는 저가 근처, 종가는 고가 근처
    if r.belt_range and rng < x.avg_range(t, r.range_lookback):
        return None
    if r.belt_week_start and b.open_time[t].isocalendar()[:2] == b.open_time[t - 1].isocalendar()[:2]:
        return None  # 그 주 첫 거래일이 아니다
    if not x.room_left(t, r.room_left) or x.support(t) is None:
        return None
    return x.setup("big_belt", t, b.l[t] - x.buf(t))


def wammie(x: Context, t: int) -> Setup | None:
    """저점을 높인 이중바닥: 같은 지지 구간을 두 번 터치(두 번째가 더 높음), 두 번째 터치에서 양봉."""
    b, r = x.b, x.r
    rng = x.ranges[t]
    if rng <= 0 or b.c[t] <= b.o[t] or (r.wammie_strong and b.c[t] < b.l[t] + rng / 2):
        return None
    tol, a = x.zones.tol(t), x.atr[t]
    if tol is None or not a:
        return None
    j = min(range(t - 2, t + 1), key=lambda i: b.l[i])  # 두 번째 터치: 최근 3봉의 최저
    if r.wammie_catalyst and (j != t or not (_kt_shape(x, t) or _shadow_shape(x, t))):
        return None  # 엄격: 두 번째 터치 봉 자체가 캥거루 꼬리·빅 섀도 모양
    lo_i, hi_i = max(1, j - r.wammie_window), j - r.wammie_gap
    if hi_i <= lo_i:
        return None
    i = min(range(lo_i, hi_i + 1), key=lambda k: b.l[k])  # 첫 터치: 그 전 범위의 최저
    if b.l[i] >= b.l[j] or min(b.l[hi_i + 1:j], default=float("inf")) < b.l[i]:
        return None  # 두 번째 저점이 더 높아야 하고, 첫 터치가 두 번째 터치 전까지의 최저여야 한다
    peak = max(range(i, j + 1), key=lambda k: b.h[k])
    if min(b.l[peak:t + 1]) < b.l[j]:
        return None  # 두 번째 터치가 반등 고점 뒤 되돌림의 최저여야 한다
    for z in reversed(x.zones.at(t)):
        if not (z.lo - tol <= b.l[i] <= z.hi and b.l[j] <= z.hi) or b.c[t] < z.lo:
            continue  # 두 터치 모두 같은 구간에
        if b.h[peak] < z.hi + r.wammie_rally_atr * a:
            continue  # 두 터치 사이에 구간 위로 반등해야 한다
        return x.setup("wammie", t, b.l[i] - x.buf(t))
    return None


def last_kiss(x: Context, t: int) -> Setup | None:
    """박스 돌파 후 박스 상단으로 돌아와(키스) 박스 밖에서 강한 양봉. 손절은 박스 중간."""
    b, r = x.b, x.r
    rng = x.ranges[t]
    if rng <= 0 or b.c[t] <= b.o[t] or (r.kiss_strong and b.c[t] < b.l[t] + rng / 2):
        return None
    for brk in range(t - 1, max(t - r.kiss_within, r.box_min_bars + 1) - 1, -1):  # 가까운 돌파부터
        tol = x.zones.tol(brk)
        if tol is None or b.c[brk] <= b.c[brk - 1]:
            continue
        box = x.box_before(brk, t)
        if box is None:
            continue
        bottom, top = box
        if not (b.c[brk] > top + tol and b.c[brk - 1] <= top + tol):
            continue  # brk가 박스를 처음 벗어난 봉이어야 한다
        if min(b.c[brk:t]) <= top:
            continue  # 돌파 뒤 박스 안으로 돌아간 적이 있다(가짜 돌파)
        if not (b.l[t] <= top + tol and b.c[t] > top):
            continue  # 박스 상단까지 내려왔다가 박스 밖에서 마감해야 한다
        return x.setup("last_kiss", t, (top + bottom) / 2, box_top=top)
    return None


def trendy_kangaroo(x: Context, t: int) -> Setup | None:
    """상승 추세 중 3~10봉 좁은 쉼에서 꼬리만 쉼 구간 아래로 튀어나온 캥거루 꼬리."""
    b, r = x.b, x.r
    rng, a = x.ranges[t], x.atr[t]
    body_lo, body_hi = min(b.o[t], b.c[t]), max(b.o[t], b.c[t])
    if rng <= 0 or not a or body_lo < b.l[t] + rng * (1 - r.tail_third):
        return None
    for n in range(r.pause_max, r.pause_min - 1, -1):  # 긴 쉼부터
        s = t - n
        if s < 1 or x.regime[s - 1] != "up":
            continue  # 쉼 직전까지 상승 추세
        p_hi, p_lo = max(b.h[s:t]), min(b.l[s:t])
        if p_hi - p_lo > r.pause_atr * a:
            continue  # 좁은 쉼이 아니다
        if b.l[t] >= p_lo or body_lo < p_lo or body_hi > p_hi:
            continue  # 몸통은 쉼 구간 안, 꼬리만 아래로
        if max(b.h[max(0, s - 10):s]) - p_lo > r.correction_atr * a:
            continue  # 큰 조정 뒤의 쉼이다
        if r.tk_zone and x.support(t) is None:
            return None  # 엄격: 지지 구간 위
        return x.setup("trendy_kangaroo", t, b.l[t] - x.buf(t))
    return None


def random_entry(x: Context, t: int) -> Setup | None:
    """대조군: 패턴과 같은 방식(봉 고가 돌파 진입·봉 저가 손절·위쪽 구간 목표)으로 무작위 봉에서 진입."""
    if random.Random(f"{x.b.instrument_id}:{t}").random() >= RANDOM_RATE:
        return None
    return x.setup("random", t, x.b.l[t] - x.buf(t))


# ----------------------------------------------------------------------------- 추세(종목별 타이밍)


def high_52w(x: Context, t: int) -> Setup | None:
    """52주 신고가: 종가가 직전 1년 최고 종가를 넘는다(신고가 근처 종목이 더 오른다는 가설)."""
    m = x.prior_max(x.window(x.tr.high_years))[t]
    if m is None or x.b.c[t] <= m:
        return None
    return x.at_open("high_52w", t)


def ma_cross(x: Context, t: int) -> Setup | None:
    """200일선 상향 돌파: 종가가 장기 이동평균을 아래에서 위로 넘는다."""
    m = x.sma(x.window(x.tr.ma_years))
    now, before = m[t], m[t - 1]
    if now is None or before is None or not (x.b.c[t] > now and x.b.c[t - 1] <= before):
        return None
    return x.at_open("ma_cross", t)


def random_hold(x: Context, t: int) -> Setup | None:
    """추세 계열 대조군: 무작위 봉에서 추세 진입과 같은 방식(다음 봉 시가·3 ATR 손절)으로 진입."""
    if random.Random(f"{x.b.instrument_id}:hold:{t}").random() >= RANDOM_RATE:
        return None
    return x.at_open("random_hold", t)


# ----------------------------------------------------------------------------- 목록


@dataclass(frozen=True)
class EntrySpec:
    kind: str
    label: str
    regime: str  # 맞는 국면(설명용)
    markets: tuple[str, ...] | None  # None = 모든 시장
    detect: Callable[[Context, int], Setup | None]
    rule: str
    control: bool = False  # 대조군(시험 대상 전략이 아님)
    family: str = "pattern"  # pattern(책 패턴) / trend(추세)


ENTRIES: dict[str, EntrySpec] = {s.kind: s for s in (
    EntrySpec("kangaroo_tail", "캥거루 꼬리", "횡보·반전", None, kangaroo_tail,
              "시가·종가가 봉 위쪽 1/3, 범위 ≥ 직전 10봉 평균, 시가·종가가 직전 봉 범위 안, 저가가 직전 7봉보다 낮음, 지지 구간에서"),
    EntrySpec("big_shadow", "빅 섀도", "횡보·반전", None, big_shadow,
              "직전 봉을 위아래로 감싼 양봉, 종가가 범위 위쪽 25%, 범위가 직전 5봉 최대, 저가가 직전 7봉보다 낮음, 지지 구간에서"),
    EntrySpec("big_belt", "빅 벨트", "반전", ("kr_stock", "us_stock"), big_belt,
              "직전 종가보다 ATR 10% 이상 낮게 시작, 시가는 저가·종가는 고가 근처(범위 20%), 범위 ≥ 직전 10봉 평균, 저가가 직전 7봉보다 낮음, "
              "지지 구간에서 (갭이 없는 24시간 코인은 제외)"),
    EntrySpec("wammie", "와미", "횡보→상승", None, wammie,
              "같은 지지 구간을 6봉 이상 간격으로 두 번 터치, 두 번째 저점이 더 높음, 사이에 구간 위로 1 ATR 이상 반등, "
              "두 번째 터치에서 강한 양봉 / 손절은 첫 터치 아래"),
    EntrySpec("last_kiss", "라스트 키스", "횡보→추세", None, last_kiss,
              "위아래 2회 이상 터치한 박스(높이 1~8 ATR, 10봉 이상)를 종가로 돌파 → 10봉 안에 박스 상단으로 돌아와 박스 밖에서 강한 양봉 "
              "/ 손절은 박스 중간, 종가가 박스 안으로 돌아오면 청산"),
    EntrySpec("trendy_kangaroo", "트렌디 캥거루", "상승 추세", None, trendy_kangaroo,
              "상승 추세(효율비) 중 3~10봉 좁은 쉼(폭 ≤ 2 ATR), 꼬리만 쉼 구간 아래로 튀어나온 캥거루 꼬리, 큰 조정 뒤가 아님"),
    EntrySpec("random", "무작위(대조군)", "-", None, random_entry,
              f"봉의 {RANDOM_RATE:.0%}를 무작위로 골라 패턴과 같은 방식으로 진입. 패턴이 이것보다 나아야 의미가 있다", control=True),
    EntrySpec("high_52w", "52주 신고가", "상승 추세", None, high_52w,
              "종가가 직전 1년(일봉 252·주봉 52봉) 최고 종가를 넘으면 다음 봉 시가 매수, 초기 손절은 종가 − 3 ATR", family="trend"),
    EntrySpec("ma_cross", "200일선 돌파", "추세 전환", None, ma_cross,
              "종가가 200일선(주봉 40주선)을 아래에서 위로 넘으면 다음 봉 시가 매수, 초기 손절은 종가 − 3 ATR", family="trend"),
    EntrySpec("random_hold", "무작위(추세 대조군)", "-", None, random_hold,
              f"봉의 {RANDOM_RATE:.0%}를 무작위로 골라 추세 진입과 같은 방식(다음 봉 시가·3 ATR 손절)으로 진입. "
              "추세 진입이 이것보다 나아야 의미가 있다", control=True, family="trend"),
)}


def entries_for(market: str, family: str | None = None) -> list[EntrySpec]:
    return [e for e in ENTRIES.values()
            if (e.markets is None or market in e.markets) and (family is None or e.family == family)]


def scan(x: Context, spec: EntrySpec) -> dict[int, Setup]:
    """종목 전체에서 패턴 봉을 찾는다. 키는 패턴 봉 번호."""
    out: dict[int, Setup] = {}
    regime_only = x.r.regime_filter and spec.kind in REVERSAL
    for t in range(x.warmup, len(x.b)):
        if x.atr[t] is None or (regime_only and x.regime[t] != "range"):
            continue  # 국면 맞춤: 반전 패턴·대조군은 횡보 국면에서만
        s = spec.detect(x, t)
        if s is not None:
            out[t] = s
    return out
