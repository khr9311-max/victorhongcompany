"""청산 방식. 계열 둘:

- 책 패턴(pattern): Naked Forex 11장(구간·분할·사다리·3봉 추적).
- 추세(trend): 50일선 이탈(종가 기준), ATR 추적(진입 뒤 최고가 − 3 ATR, 샹들리에 청산).

손절은 진입 규칙이 정한 초기 손절에서 시작해 내려가지 않는다. 손절 이동은 봉 종가 뒤에 정하고 다음 봉부터 적용한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aifund.lab.setups import Context, Setup

MIN_RR = 1.0  # 목표 구간은 위험(진입가 - 손절가)의 1배 이상 떨어진 것(책 1단계 '위험 대비 보상', 물라 예시)
FALLBACK_R = 2.0  # 위쪽에 쓸 구간이 없으면 위험의 2배를 목표로 둔다(책에 없는 보완)


@dataclass(frozen=True)
class ExitSpec:
    kind: str
    label: str
    rule: str
    family: str = "pattern"


EXITS: dict[str, ExitSpec] = {s.kind: s for s in (
    ExitSpec("zone", "구간 청산", f"위쪽 첫 구간(위험의 {MIN_RR:g}배 이상 떨어진 것) 하단에서 전량. 쓸 구간이 없으면 위험의 {FALLBACK_R:g}배"),
    ExitSpec("split", "분할 청산", "절반은 첫 목표, 나머지는 손절을 본전으로 올리고 그다음 구간에서"),
    ExitSpec("ladder", "사다리 청산", "진입 뒤 닿은 구간이 하나면 손절을 본전으로, 둘 이상이면 닿은 구간 중 두 번째로 높은 구간으로 올림. "
                                     "구간은 매 봉 그 시점 기준(새로 생긴 구간 포함)"),
    ExitSpec("three_bar", "3봉 추적", "패턴 뒤 3봉째부터 최근 3봉 최저가 아래로 손절을 계속 올림"),
    ExitSpec("ma_exit", "50일선 이탈", "종가가 50일선(주봉 10주선) 아래로 내려가면 청산. 초기 손절(3 ATR)은 그대로 둠", family="trend"),
    ExitSpec("atr_trail", "ATR 추적", "진입 뒤 최고가 − 3 ATR로 손절을 계속 올림(샹들리에 청산)", family="trend"),
)}


@dataclass(frozen=True)
class Costs:
    fee: float  # 편도 비용률(국내주식 매도세는 절반씩 양쪽)
    slip: float  # 편도 슬리피지(불리한 쪽)


@dataclass
class Position:
    setup: Setup
    exit_kind: str
    entry_bar: int
    entry_px: float  # 기준 체결가(슬리피지 전)
    cost: float  # 진입에 쓴 현금(수수료 포함)
    units0: float
    units: float
    stop: float
    risk: float  # 진입가 - 초기 손절(가격)
    targets: list[tuple[float, float, str]]  # (가격, 처음 수량 대비 비율, 사유) 오름차순
    high: float  # 진입 후 최고가
    mid_bar: bool = False  # 봉 중간(역지정가)에 진입했나. 그 봉의 시가는 진입 전 가격이다
    proceeds: float = 0.0
    sold_value: float = 0.0  # Σ 수량 × 기준 체결가(평균 청산가 계산용)
    sold_units: float = 0.0
    reasons: list[str] = field(default_factory=list)
    partial: bool = False  # 분할 청산 1차 체결

    @property
    def closed(self) -> bool:
        return self.units <= self.units0 * 1e-9


def open_position(setup: Setup, exit_kind: str, bar: int, px: float, cash: float, costs: Costs,
                  atr_now: float | None, *, mid_bar: bool = False, min_rr: float = MIN_RR) -> Position:
    if exit_kind not in EXITS:
        raise ValueError(f"알 수 없는 청산 방식 {exit_kind}")
    risk = px - setup.stop if px > setup.stop else max(atr_now or 0.0, px * 1e-4)
    zones = [z for z in setup.targets if z > px]
    qualified = [z for z in zones if z - px >= min_rr * risk]
    first = qualified[0] if qualified else px + FALLBACK_R * risk
    targets: list[tuple[float, float, str]] = []
    if exit_kind == "zone":
        targets = [(first, 1.0, "목표 구간")]
    elif exit_kind == "split":
        nxt = [z for z in zones if z > first]
        second = nxt[0] if nxt else first + (first - px)
        targets = [(first, 0.5, "1차 목표"), (second, 0.5, "2차 목표")]
    units = cash / (px * (1 + costs.slip) * (1 + costs.fee))
    return Position(setup, exit_kind, bar, px, cash, units, units, setup.stop, risk, targets, px, mid_bar)


def sell(pos: Position, frac: float, px: float, costs: Costs, why: str) -> float:
    """처음 수량 대비 frac만큼(남은 수량 한도) 판다. 받은 현금을 돌려준다."""
    qty = min(pos.units, frac * pos.units0)
    if qty <= 0:
        return 0.0
    value = qty * px * (1 - costs.slip) * (1 - costs.fee)
    pos.units -= qty
    pos.proceeds += value
    pos.sold_value += qty * px
    pos.sold_units += qty
    if why not in pos.reasons:
        pos.reasons.append(why)
    if pos.exit_kind == "split" and why == "1차 목표":
        pos.partial = True
    return value


def close_exit(pos: Position, x: Context, j: int) -> str | None:
    """종가 기준 청산 규칙(손절과 별개). 해당하면 사유를 돌려준다."""
    c = x.b.c[j]
    if pos.setup.box_top is not None and c < pos.setup.box_top:
        return "박스 안으로 마감"
    if (x.r.kt_cut75 and pos.setup.kind == "kangaroo_tail" and pos.entry_px > pos.setup.stop
            and c <= pos.entry_px - 0.75 * (pos.entry_px - pos.setup.stop)):
        return "75% 컷"  # 책 8장: 손절까지 75% 밀리면 미리 정리
    if pos.exit_kind == "ma_exit":
        m = x.sma(x.window(x.tr.exit_ma_years))[j]
        if m is not None and c < m:
            return "이평 이탈"
    return None


def update_stop(pos: Position, x: Context, j: int) -> None:
    """봉 j 종가 뒤 손절 갱신(다음 봉부터 적용). 손절은 내려가지 않는다."""
    b = x.b
    pos.high = max(pos.high, b.h[j])
    new = pos.stop
    if pos.exit_kind == "split" and pos.partial:
        new = max(new, pos.entry_px)  # 1차 목표 뒤 본전
    elif pos.exit_kind == "ladder":
        # 그 시점 구간 중 진입가 위에서 진입 후 최고가가 닿은 것(책: 차트에 새 구간을 계속 그린다)
        reached = sorted(z.lo for z in x.zones.at(j) if pos.entry_px < z.lo <= pos.high)
        if reached:
            new = max(new, pos.entry_px if len(reached) == 1 else reached[-2])
    elif pos.exit_kind == "three_bar" and j >= pos.setup.t + 3:
        new = max(new, min(b.l[j - 2:j + 1]) - x.buf(j))
    elif pos.exit_kind == "atr_trail" and x.atr[j]:
        new = max(new, pos.high - x.tr.stop_atr * x.atr[j])  # type: ignore[operator]
    pos.stop = new
