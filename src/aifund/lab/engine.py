"""한 종목·한 변형(진입 × 청산) 시뮬레이션. 체결 가정은 두 가지다.

- bar_close(현 시스템 방식): 봉이 끝난 뒤 판단하고 다음 봉 시가에 체결한다. 운용 사이클이 실제로 할 수 있는 방식이다.
  진입은 종가가 진입가(패턴 봉 고가 위)를 넘었을 때, 손절은 종가가 손절가 이하일 때, 목표는 고가가 목표에 닿았을 때.
  업비트 연동은 거래소 측 손절 주문을 쓰지 않으므로(brokers/upbit.py) 장중 손절을 가정하지 않는다.
- intrabar(책 방식): 책대로 역지정가 매수·손절·목표 지정가가 봉 안에서 그 가격에 체결된다고 본다(갭이면 시가).
  한 봉에서 손절과 목표가 모두 닿으면 손절이 먼저라고 보고, 진입한 봉에서 손절가에 닿으면 진입 뒤 손절로 본다(보수적).

슬리브 하나(시작 1.0)를 전액 진입·청산한다. 종목당 동시에 한 포지션만 갖고, 보유 중 나온 패턴은 무시한다.
패턴은 봉 종가에 확정되고 그 뒤 Setup.window봉(기본 3, 엄격 해석의 캥거루 꼬리·빅 섀도는 1) 동안만 유효하다.
추세 계열 신호(Setup.at_open)는 확인 없이 두 체결 가정 모두 다음 봉 시가에 진입한다. 종가 기준 청산(박스 안 마감·
이평 이탈·75% 컷)은 책 방식이면 그 종가, 현 시스템 방식이면 다음 봉 시가에 체결한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aifund.lab.exits import Costs, Position, close_exit, open_position, sell, update_stop
from aifund.lab.setups import Context, Setup

MODELS = ("bar_close", "intrabar")
MODEL_LABELS = {"bar_close": "현 시스템 방식", "intrabar": "책 방식"}


@dataclass
class Trade:
    variant: str
    model: str
    instrument_id: str
    entry: str
    exit: str
    setup_time: datetime
    entry_time: datetime
    exit_time: datetime
    entry_px: float
    exit_px: float
    stop0: float
    ret: float  # 비용 차감 수익률
    r_mult: float | None  # 수익률 ÷ 초기 위험률(손절이 없는 순환 계열은 None)
    bars: int
    reason: str
    regime: str | None  # 패턴 봉 시점 국면


@dataclass
class SimState:
    """마지막 봉 종가 시점의 모델 상태(운용 연결이 실제 보유와 맞출 때 쓴다)."""

    pos: Position | None  # 보유 중인 포지션(분할 청산 뒤면 남은 수량)
    pending_entry: Setup | None  # 마지막 종가에 정한 진입(다음 봉 시가 체결 예정)
    pending_exit: list[tuple[float, str]]  # 마지막 종가에 정한 청산(처음 수량 대비 비율, 사유)
    armed: Setup | None  # 진입 확인을 기다리는 패턴

    @property
    def remaining(self) -> float:
        """정해진 청산까지 반영한 뒤 남을 비율(처음 수량 대비, 0~1)."""
        if self.pos is None:
            return 0.0
        left = self.pos.units / self.pos.units0
        for frac, _ in self.pending_exit:
            left -= min(frac, left)
        return max(0.0, left)


@dataclass
class SimResult:
    trades: list[Trade]
    equity: list[float]  # 봉 종가 기준 슬리브 가치(시작 1.0)
    exposure: float  # 보유 봉 비율
    state: SimState | None = None  # keep_open일 때만


def _trade(x: Context, pos: Position, exit_bar: int, model: str, variant: str, exit_time: datetime) -> Trade:
    b, s = x.b, pos.setup
    ret = pos.proceeds / pos.cost - 1
    # R은 신호 시점에 계획한 위험(진입가 − 손절가)으로 잰다. 갭으로 실제 진입가가 손절가에 붙어도 튀지 않게
    plan = (s.trigger - s.stop) / s.trigger if s.trigger > s.stop > 0 else None
    return Trade(variant, model, b.instrument_id, s.kind, pos.exit_kind, b.close_time[s.t], b.open_time[pos.entry_bar],
                 exit_time, pos.entry_px, pos.sold_value / pos.sold_units if pos.sold_units else pos.entry_px, s.stop,
                 ret, ret / plan if plan else None, exit_bar - pos.entry_bar, "+".join(pos.reasons), x.regime[s.t])


def simulate(x: Context, setups: dict[int, Setup], exit_kind: str, model: str, costs: Costs, *, variant: str,
             entry_window: int | None = None, keep_open: bool = False) -> SimResult:
    """entry_window를 주지 않으면 패턴마다 정한 진입 유효 봉 수(Setup.window)를 쓴다.

    keep_open이면 데이터 끝에서 보유를 정리하지 않고 마지막 종가 시점의 모델 상태(SimResult.state)를 돌려준다.
    """
    if model not in MODELS:
        raise ValueError(f"알 수 없는 체결 가정 {model}")
    b = x.b
    cash = 1.0
    pos: Position | None = None
    armed: Setup | None = None
    pending_entry: Setup | None = None
    pending_exit: list[tuple[float, str]] = []
    trades: list[Trade] = []
    equity: list[float] = []
    held = 0
    for j in range(len(b)):
        if pos is None and armed is None and pending_entry is None:
            s = setups.get(j)  # 빠른 경로: 대기·보유 없음
            if s is not None:
                if s.at_open:
                    pending_entry = s
                else:
                    armed = s
            equity.append(cash)
            continue
        o, h, lo, c = b.o[j], b.h[j], b.l[j], b.c[j]
        if model == "bar_close":
            # 1) 지난 봉 종가에 정한 주문을 이번 봉 시가에 실행
            if pos is not None and pending_exit:
                for frac, why in pending_exit:
                    cash += sell(pos, frac, o, costs, why)
                pending_exit = []
                if pos.closed:
                    trades.append(_trade(x, pos, j, model, variant, b.open_time[j]))
                    pos = None
            if pos is None and pending_entry is not None:
                pos = open_position(pending_entry, exit_kind, j, o, cash, costs, x.atr[j], min_rr=x.r.min_rr)
                cash = 0.0
                pending_entry = None
            # 2) 이번 봉 종가로 판단
            if pos is not None:
                why_close = None if c <= pos.stop else close_exit(pos, x, j)
                if c <= pos.stop:
                    pending_exit = [(1.0, "손절")]
                elif why_close:
                    pending_exit = [(1.0, why_close)]
                else:
                    hit = [tg for tg in pos.targets if h >= tg[0]]
                    for tg in hit:
                        pos.targets.remove(tg)
                        pending_exit.append((tg[1], tg[2]))
                    if hit and pos.exit_kind == "split" and hit[0][2] == "1차 목표":
                        pos.partial = True
                    update_stop(pos, x, j)
            elif armed is not None and pending_entry is None and j > armed.t:
                if c > armed.trigger:
                    pending_entry, armed = armed, None
                elif c <= armed.stop or j >= armed.t + (entry_window or armed.window):
                    armed = None
        else:
            # 1) 시가 진입(추세 계열) 또는 대기 중인 역지정가 매수
            if pos is None and pending_entry is not None:
                pos = open_position(pending_entry, exit_kind, j, o, cash, costs, x.atr[j], min_rr=x.r.min_rr)
                cash = 0.0
                pending_entry = None
            elif pos is None and armed is not None and j > armed.t:
                if h >= armed.trigger:
                    pos = open_position(armed, exit_kind, j, max(o, armed.trigger), cash, costs, x.atr[j],
                                        mid_bar=o < armed.trigger, min_rr=x.r.min_rr)
                    cash = 0.0
                    armed = None
                elif lo <= armed.stop:
                    armed = None  # 진입 전에 손절가를 깼다
            # 2) 보유 중 장중 청산(손절 우선)
            if pos is not None:
                first = j == pos.entry_bar and pos.mid_bar  # 봉 중간 진입이면 그 봉의 시가는 진입 전 가격
                if lo <= pos.stop:
                    cash += sell(pos, 1.0, pos.stop if first else min(o, pos.stop), costs, "손절")
                else:
                    for tg in [tg for tg in pos.targets if h >= tg[0]]:
                        pos.targets.remove(tg)
                        cash += sell(pos, tg[1], tg[0] if first else max(o, tg[0]), costs, tg[2])
                    if not pos.closed:
                        why_close = close_exit(pos, x, j)
                        if why_close:
                            cash += sell(pos, 1.0, c, costs, why_close)
                        else:
                            update_stop(pos, x, j)
                if pos.closed:
                    trades.append(_trade(x, pos, j, model, variant, b.close_time[j]))
                    pos = None
            if armed is not None and j >= armed.t + (entry_window or armed.window):
                armed = None
        # 3) 이번 봉 종가에 확정된 새 신호 → 진입 대기(보유·진입 예정이면 무시)
        if pos is None and pending_entry is None:
            s = setups.get(j)
            if s is not None:
                if s.at_open:
                    pending_entry, armed = s, None
                else:
                    armed = s
        equity.append(cash + (pos.units * c if pos is not None else 0.0))
        held += pos is not None
    exposure = held / len(b) if len(b) else 0.0
    if keep_open:
        return SimResult(trades, equity, exposure, SimState(pos, pending_entry, list(pending_exit), armed))
    if pos is not None:  # 데이터 끝: 마지막 종가로 정리해 거래로 남긴다
        last = len(b) - 1
        cash += sell(pos, 1.0, b.c[last], costs, "데이터 끝")
        trades.append(_trade(x, pos, last, model, variant, b.close_time[last]))
        equity[-1] = cash
    return SimResult(trades, equity, exposure)


def buy_hold(x: Context, costs: Costs) -> list[float]:
    """기준선: 첫 봉 시가에 전액 매수해 보유(슬리브 시작 1.0)."""
    b = x.b
    units = 1.0 / (b.o[0] * (1 + costs.slip) * (1 + costs.fee))
    return [units * c for c in b.c]
