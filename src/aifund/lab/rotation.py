"""순환 계열: 종목 간 상대강도로 매월 보유 종목을 고른다(포트폴리오 단위).

- 판단: 매월 마지막 봉 종가. 체결: 다음 봉 시가(현 시스템 방식과 같은 시점 구조). 장중 주문이 없어 체결 가정은 하나다.
- 점수: 최근 12개월 수익률에서 마지막 1개월을 뺀 것(12-1 모멘텀, 단기 반전 효과 제외). 1년 이상 데이터가 있는 종목만.
- 상위 1/4(최소 1종목)을 동일비중으로 보유. 듀얼 모멘텀은 점수가 0 이하인 종목 몫을 현금으로 둔다.
- 대조군: 그때 거래되는 전 종목 동일비중 월간 재조정(종목 선택 없이 같은 재조정 비용을 낸다).
- 비용: 재조정 때 사고판 금액에만 수수료·슬리피지. 거래 기록은 종목을 새로 담았다가 모두 판 구간 하나를 한 거래로 본다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aifund.lab.bars import Bars, bars_per_year, regimes
from aifund.lab.engine import Trade
from aifund.lab.exits import Costs


@dataclass(frozen=True)
class RotationSpec:
    kind: str
    label: str
    rule: str
    select: bool = True  # False면 전 종목(대조군)
    absolute: bool = False  # 듀얼 모멘텀: 점수 > 0일 때만 보유
    control: bool = False
    lookback_years: float = 1.0
    skip_years: float = 1 / 12
    top_frac: float = 0.25


ROTATIONS: dict[str, RotationSpec] = {s.kind: s for s in (
    RotationSpec("rs_top", "상대강도 상위 1/4",
                 "매월 마지막 봉 종가 기준 12-1개월 수익률 상위 1/4 종목을 동일비중 보유, 다음 봉 시가에 교체"),
    RotationSpec("dual_momentum", "듀얼 모멘텀",
                 "상대강도 상위 1/4 중 12-1개월 수익률이 0보다 큰 종목만 보유, 나머지 몫은 현금(하락장 회피)", absolute=True),
    RotationSpec("equal_monthly", "동일비중 월간 재조정(대조군)",
                 "거래되는 전 종목을 동일비중으로 매월 재조정. 순환 전략이 이것보다 나아야 종목 선택이 의미 있다",
                 select=False, control=True),
)}


@dataclass
class _Holding:
    units: float = 0.0
    entry_px: float = 0.0
    entry_bar: int = 0
    entry_time: datetime | None = None
    setup_time: datetime | None = None
    regime: str | None = None


def simulate_rotation(bars_list: list[Bars], spec: RotationSpec, costs: Costs, timeline: list[datetime], *,
                      variant: str) -> tuple[list[Trade], list[float], float]:
    """(거래, timeline 기준 자산 곡선(시작 1.0), 평균 투입 비율)."""
    n = len(bars_list)
    g_len = len(timeline)
    bpy = bars_per_year(bars_list[0].market, bars_list[0].interval)
    look, skip = round(spec.lookback_years * bpy), round(spec.skip_years * bpy)
    regs = [regimes(b.c) for b in bars_list]
    at: list[list[int | None]] = []  # at[k][g] = 종목 k의 봉 번호(그 시각에 봉이 있을 때만)
    last: list[list[int]] = []  # last[k][g] = 그 시각까지의 마지막 봉 번호(-1 = 아직 없음)
    for b in bars_list:
        pos_of = {t: i for i, t in enumerate(b.close_time)}
        row_at: list[int | None] = []
        row_last: list[int] = []
        cur = -1
        for t in timeline:
            i = pos_of.get(t)
            if i is not None:
                cur = i
            row_at.append(i)
            row_last.append(cur)
        at.append(row_at)
        last.append(row_last)
    cash = 1.0
    hold = [_Holding() for _ in range(n)]
    trades: list[Trade] = []
    curve: list[float] = []
    invested_sum = 0.0

    def mark(k: int, g: int) -> float:
        i = last[k][g]
        return 0.0 if i < 0 or hold[k].units == 0 else hold[k].units * bars_list[k].c[i]

    def close_trade(k: int, px: float, i: int, when: datetime, why: str) -> None:
        h, b = hold[k], bars_list[k]
        ret = px * (1 - costs.slip) * (1 - costs.fee) / (h.entry_px * (1 + costs.slip) * (1 + costs.fee)) - 1
        trades.append(Trade(variant, "bar_close", b.instrument_id, spec.kind, "rebalance", h.setup_time or when,
                            h.entry_time or when, when, h.entry_px, px, 0.0, ret, None, i - h.entry_bar, why, h.regime))

    pending: dict[int, float] | None = None  # 다음 봉 시가에 맞출 목표 비중
    decided_at = 0
    for g in range(g_len):
        if pending is not None:
            # 1) 지난 봉 종가에 정한 목표 비중을 이번 봉 시가에 맞춘다(봉이 없는 종목은 그대로 둔다)
            prices: dict[int, float] = {}
            for k in range(n):
                i = at[k][g]
                if i is not None:
                    prices[k] = bars_list[k].o[i]
            equity = cash + sum(hold[k].units * prices[k] if k in prices else mark(k, g - 1) for k in range(n))
            buys: dict[int, float] = {}
            for k, px in prices.items():
                diff = pending.get(k, 0.0) * equity - hold[k].units * px
                if diff < 0:  # 먼저 팔아 현금 확보
                    qty = min(hold[k].units, -diff / px)
                    cash += qty * px * (1 - costs.slip) * (1 - costs.fee)
                    hold[k].units -= qty
                    if hold[k].units <= 1e-12:
                        i = at[k][g]
                        assert i is not None
                        close_trade(k, px, i, bars_list[k].open_time[i], "교체")
                        hold[k] = _Holding()
                elif diff > 0:
                    buys[k] = diff
            need = sum(v * (1 + costs.slip) * (1 + costs.fee) for v in buys.values())
            scale = min(1.0, cash / need) if need > 0 else 0.0
            for k, value in buys.items():
                px, i = prices[k], at[k][g]
                assert i is not None
                qty = value * scale / px
                cash -= qty * px * (1 + costs.slip) * (1 + costs.fee)
                if hold[k].units == 0:
                    b = bars_list[k]
                    hold[k] = _Holding(0.0, px, i, b.open_time[i], timeline[decided_at], regs[k][last[k][decided_at]])
                hold[k].units += qty
            cash = max(cash, 0.0)
            pending = None
        # 2) 달이 바뀌기 직전 봉의 종가에 다음 달 보유를 정한다
        if g + 1 < g_len and timeline[g + 1].month != timeline[g].month:
            scores: dict[int, float] = {}
            for k in range(n):
                i = last[k][g]
                if i < 0 or at[k][g] is None:
                    continue  # 그 시각에 거래되지 않은 종목은 고르지 않는다
                if not spec.select:
                    scores[k] = 0.0
                elif i - look >= 0:
                    c = bars_list[k].c
                    scores[k] = c[i - skip] / c[i - look] - 1
            if scores:
                if spec.select:
                    slots = max(1, round(spec.top_frac * len(scores)))
                    top = sorted(scores, key=lambda k: -scores[k])[:slots]
                    chosen = [k for k in top if not spec.absolute or scores[k] > 0]
                else:
                    slots, chosen = len(scores), list(scores)
                pending = {k: 1.0 / slots for k in chosen}
                decided_at = g
        value = sum(mark(k, g) for k in range(n))
        curve.append(cash + value)
        invested_sum += value / (cash + value) if cash + value > 0 else 0.0
    for k in range(n):  # 데이터 끝: 마지막 종가로 정리
        if hold[k].units > 0:
            i = last[k][g_len - 1]
            b = bars_list[k]
            cash += hold[k].units * b.c[i] * (1 - costs.slip) * (1 - costs.fee)
            close_trade(k, b.c[i], i, b.close_time[i], "데이터 끝")
    if curve:
        curve[-1] = cash
    return trades, curve, invested_sum / g_len if g_len else 0.0
