"""전략 연구소 변형을 운용 전략으로 쓰는 연결부(설정 strategies.lab, 전략 id는 'lab:변형 id').

연구소와 운용이 같은 코드를 쓴다 — 과거 데이터로 시험한 규칙과 실제 판단이 어긋나지 않게.

- 종목별 타이밍(책 패턴·추세): 스냅샷의 완성봉으로 연구소 시뮬레이션(현 시스템 방식: 봉 마감 판단 → 다음 봉 시가)을
  다시 돌려, 마지막 봉 종가 시점의 모델 상태를 실제 보유와 맞춘다.
  · 마지막 종가에 진입이 정해졌고 보유가 없으면 매수(종목 몫 전부).
  · 마지막 종가에 청산이 정해졌으면 매도(분할 청산이면 남을 몫으로 축소).
  · 모델이 보유 중이면 유지. 모델은 보유 중인데 실제로 없으면 따라 사지 않는다(놓친 신호를 쫓지 않음).
  · 모델이 비어 있는데 실제 보유가 있으면 매도해 모델과 맞춘다.
- 순환(상대강도): 가장 최근 월말 봉 종가로 고른 종목을 동일비중 목표로 낸다. 마지막 완성봉이 월말 봉이면(달이 바뀐 첫 판단)
  비중을 다시 맞추고, 그 사이에는 고른 종목은 유지, 빠진 종목은 매도, 아직 못 산 고른 종목만 산다.

손절·청산은 봉 마감 때만 판단한다(거래소 측 손절 주문 없음 — 연구소 '현 시스템 방식'과 같은 가정).
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

from aifund.core.money import ZERO
from aifund.data.collector import InstrumentSnap
from aifund.domain.models import Action, Candle
from aifund.lab.bars import Bars, bars_per_year
from aifund.lab.catalog import Variant, find
from aifund.lab.engine import SimState, simulate
from aifund.lab.exits import Costs
from aifund.lab.setups import INTERPRETATIONS, RULES_VERSION, Context, TrendRules, scan
from aifund.strategies.base import REGISTRY, PositionView, Signal, Strategy, StrategyContext

if TYPE_CHECKING:
    from aifund.config.settings import StrategySettings

PREFIX = "lab:"
_STATES: OrderedDict[tuple[Any, ...], tuple[SimState, Bars]] = OrderedDict()  # 장부 넷이 같은 스냅샷을 쓰므로 한 번만 계산
_STATES_MAX = 512


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def is_lab(strategy_id: str) -> bool:
    return strategy_id.startswith(PREFIX)


def _num(x: float) -> float:
    return float(f"{x:.6g}")


class LabStrategy(Strategy):
    variant: ClassVar[Variant]
    params_model = NoParams
    version = RULES_VERSION

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.market = "kr_stock"
        self.interval = "1d"

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        self.market, self.interval = ctx.snapshot.market, ctx.snapshot.interval
        return super().evaluate(ctx)

    def window(self, years: float) -> int:
        return round(years * bars_per_year(self.market, self.interval))


def model_state(v: Variant, it: InstrumentSnap) -> tuple[SimState, Bars]:
    """마지막 완성봉 종가 시점의 모델 상태. 같은 봉 묶음이면 기억해 둔 결과를 쓴다."""
    cs = it.candles
    key = (v.id, it.instrument.instrument_id, cs[0].interval, len(cs), cs[0].open_time, cs[-1].close_time, cs[-1].close)
    hit = _STATES.get(key)
    if hit is not None:
        _STATES.move_to_end(key)
        return hit
    assert v.entry is not None and v.exit is not None
    bars = Bars.from_candles(cs)
    x = Context.for_interp(bars, INTERPRETATIONS[v.interp])
    res = simulate(x, scan(x, v.entry), v.exit.kind, "bar_close", Costs(0.0, 0.0), variant=v.id, keep_open=True)
    assert res.state is not None
    _STATES[key] = (res.state, bars)
    while len(_STATES) > _STATES_MAX:
        _STATES.popitem(last=False)
    return res.state, bars


class LabTiming(LabStrategy):
    """책 패턴·추세(종목별 타이밍)."""

    def min_bars(self) -> int:
        v, tr = self.variant, TrendRules()
        need = 60  # 지표·구간이 자리 잡을 최소 봉 수(연구소 실행과 같음)
        assert v.entry is not None and v.exit is not None
        if v.entry.kind == "high_52w":
            need = max(need, self.window(tr.high_years) + 2)
        if v.entry.kind == "ma_cross":
            need = max(need, self.window(tr.ma_years) + 2)
        if v.exit.kind == "ma_exit":
            need = max(need, self.window(tr.exit_ma_years) + 2)
        return need

    def decide(self, it: InstrumentSnap, pos: PositionView, share: Decimal, ctx: StrategyContext) -> Signal:
        v, sid, iid = self.variant, self.strategy_id, it.instrument.instrument_id
        st, bars = model_state(v, it)
        held = pos.qty > 0
        label = v.label
        info: dict[str, Any] = {"model": "보유" if st.pos else ("진입 예정" if st.pending_entry else "없음"), "bars": len(bars)}
        inval = ""
        if st.pos is not None:
            info.update(entry_px=_num(st.pos.entry_px), stop=_num(st.pos.stop))
            inval = f"종가가 손절 {st.pos.stop:,.6g} 이하이면 청산"
        rem = st.remaining
        if st.pending_exit and st.pos is not None:
            why = "+".join(w for _, w in st.pending_exit)
            if rem <= 1e-9:
                return Signal(sid, iid, Action.SELL if held else Action.WAIT, ZERO, f"{label}: 청산 결정({why})", info)
            return Signal(sid, iid, Action.REDUCE, share * Decimal(f"{rem:.6f}"), f"{label}: 일부 청산({why}), 남길 몫 {rem:.0%}",
                          info, inval)
        if st.pending_entry is not None:
            s = st.pending_entry
            info.update(signal_time=bars.close_time[s.t].isoformat(), stop=_num(s.stop))
            if held:
                return Signal(sid, iid, Action.HOLD, None, f"{label}: 진입 신호이나 이미 보유", info)
            how = "다음 봉 시가 진입" if s.at_open else f"종가 {bars.c[-1]:,.6g} > 진입가 {s.trigger:,.6g} 확인"
            return Signal(sid, iid, Action.BUY, share, f"{label}: {bars.close_time[s.t]:%Y-%m-%d %H:%M} 신호 → {how}", info,
                          f"종가가 손절 {s.stop:,.6g} 이하이면 청산")
        if st.pos is not None:
            if not held:
                return Signal(sid, iid, Action.WAIT, ZERO, f"{label}: 모델은 보유 중이지만 진입 시점이 지나 따라 사지 않음", info)
            if rem < 1 - 1e-9:  # 분할 청산 뒤 남은 몫으로 맞춘다(늘리지는 않음)
                return Signal(sid, iid, Action.REDUCE, share * Decimal(f"{rem:.6f}"), f"{label}: 남은 몫 {rem:.0%} 유지", info,
                              inval)
            return Signal(sid, iid, Action.HOLD, None, f"{label}: 보유 유지", info, inval)
        if held:
            return Signal(sid, iid, Action.SELL, ZERO, f"{label}: 모델 기준 보유 없음 → 정리", info)
        if st.armed is not None:
            info.update(trigger=_num(st.armed.trigger))
            return Signal(sid, iid, Action.WAIT, ZERO, f"{label}: 패턴 확인 대기(진입가 {st.armed.trigger:,.6g})", info)
        return Signal(sid, iid, Action.WAIT, ZERO, f"{label}: 신호 없음", info)


def month_end_index(cs: list[Candle], now: datetime) -> int | None:
    """가장 최근 '월말 봉'(다음 봉 또는 지금이 다른 달인 봉)의 번호."""
    nxt = (now.year, now.month)
    for i in range(len(cs) - 1, -1, -1):
        cur = (cs[i].close_time.year, cs[i].close_time.month)
        if cur != nxt:
            return i
        nxt = cur
    return None


class LabRotation(LabStrategy):
    """순환(종목 간 상대강도). 목표 비중은 이 전략 슬리브 전체 대비."""

    def min_bars(self) -> int:
        assert self.variant.rotation is not None
        return self.window(self.variant.rotation.lookback_years) + 1

    def decide(self, it: InstrumentSnap, pos: PositionView, share: Decimal, ctx: StrategyContext) -> Signal:
        raise NotImplementedError("순환 전략은 종목을 함께 보고 evaluate에서 판단한다")

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        snap = ctx.snapshot
        self.market, self.interval = snap.market, snap.interval
        spec = self.variant.rotation
        assert spec is not None
        look, skip = self.window(spec.lookback_years), self.window(spec.skip_years)
        scores: dict[str, float] = {}
        short: dict[str, str] = {}
        fresh = False  # 마지막 완성봉이 월말 봉 = 이번 달 재조정 판단
        for iid, it in snap.items.items():
            cs = it.candles
            i_end = month_end_index(cs, ctx.now) if cs else None
            if i_end is None:
                short[iid] = "월말 봉 없음"
                continue
            fresh = fresh or i_end == len(cs) - 1
            if not spec.select:
                scores[iid] = 0.0
            elif i_end - look >= 0:
                scores[iid] = float(cs[i_end - skip].close) / float(cs[i_end - look].close) - 1
            else:
                short[iid] = f"데이터 부족: 월말 기준 {i_end + 1}봉 < 필요 {look + 1}봉"
        chosen: list[str] = []
        slots = 1
        if scores:
            if spec.select:
                slots = max(1, round(spec.top_frac * len(scores)))
                top = sorted(scores, key=lambda k: -scores[k])[:slots]
                chosen = [k for k in top if not spec.absolute or scores[k] > 0]
            else:
                slots, chosen = len(scores), list(scores)
        weight = Decimal(1) / Decimal(slots)
        out: list[Signal] = []
        label = self.variant.label
        for iid, it in snap.items.items():
            held = ctx.pos(iid).qty > 0
            sc = scores.get(iid)
            info = {"score_12_1": None if sc is None else _num(sc), "slots": slots, "chosen": iid in chosen,
                    "rebalance": fresh}
            if iid in chosen and (fresh or not held):
                why = "월말 재조정" if fresh else "선정 종목 미보유분 매수"
                if it.ok:
                    out.append(Signal(self.strategy_id, iid, Action.BUY, weight,
                                      f"{label}: {why}(12-1개월 {sc:+.1%}, {slots}자리 중 하나)", info, "다음 월말에 다시 고름"))
                else:
                    out.append(Signal(self.strategy_id, iid, Action.HOLD if held else Action.WAIT, None if held else ZERO,
                                      "데이터 품질 미달로 신규 매수 보류: " + "; ".join(it.issues), info))
            elif iid in chosen:
                out.append(Signal(self.strategy_id, iid, Action.HOLD, None, f"{label}: 선정 유지(12-1개월 {sc:+.1%})", info))
            elif held:
                out.append(Signal(self.strategy_id, iid, Action.SELL, ZERO, f"{label}: 이번 달 선정에서 빠짐 → 매도", info))
            else:
                why = short.get(iid) or ("절대 모멘텀 0 이하" if spec.absolute and sc is not None and sc <= 0 else "선정 안 됨")
                out.append(Signal(self.strategy_id, iid, Action.WAIT, ZERO, f"{label}: {why}", info))
        return out


_CLASSES: dict[str, type[LabStrategy]] = {}


def lab_class(variant_id: str) -> type[LabStrategy]:
    """변형마다 전략 클래스를 하나씩 만든다(전략 id·제목·가설이 클래스 속성인 기존 구조에 맞춤)."""
    cls = _CLASSES.get(variant_id)
    if cls is None:
        v = find(variant_id)
        if v is None:
            raise KeyError(f"알 수 없는 연구소 전략 {variant_id} (aifund lab catalog 참고)")
        if v.rotation is not None:
            base: type[LabStrategy] = LabRotation
            rule = v.rotation.rule
        else:
            assert v.entry is not None and v.exit is not None
            base, rule = LabTiming, f"{v.entry.rule} / 청산: {v.exit.rule}"
        cls = type(f"Lab{len(_CLASSES)}", (base,), {"variant": v, "strategy_id": PREFIX + v.id,
                                                     "title": f"연구소 · {v.label}", "hypothesis": rule})
        _CLASSES[variant_id] = cls
    return cls


def lab_strategies(settings: "StrategySettings", market: str) -> list[Strategy]:
    """이 시장에서 켜진 연구소 전략."""
    return [lab_class(vid)() for vid, tg in settings.lab.items() if tg.enabled and market in tg.markets]


def strategy_version(strategy_id: str) -> str:
    if is_lab(strategy_id):
        return lab_class(strategy_id[len(PREFIX):]).version
    from aifund.strategies import mean_reversion, trend  # noqa: F401  (등록)

    return REGISTRY[strategy_id].version


def strategy_label(strategy_id: str) -> str | None:
    if is_lab(strategy_id):
        v = find(strategy_id[len(PREFIX):])
        return None if v is None else f"연구소 {v.label}"
    return None
