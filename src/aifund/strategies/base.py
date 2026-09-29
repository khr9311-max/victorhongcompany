"""전략 공통 인터페이스와 등록부.

모든 전략은 완성된 캔들만 사용하고, 같은 인터페이스(evaluate)로 종목별 신호를 낸다.
허용 행동: 매수·보유·축소·매도·관망(현물 전용, 공매도·신용·선물·마틴게일 없음).
데이터 품질 문제가 있으면 매수 신호는 관망으로 바뀐다(검사 자동 완화 없음). 매도(위험 감소)는 유지된다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel

from aifund.data.collector import InstrumentSnap, Snapshot
from aifund.domain.models import Action


@dataclass(frozen=True)
class PositionView:
    qty: Decimal
    cost_basis: Decimal
    opened_at: datetime | None

    @property
    def avg_cost(self) -> Decimal | None:
        return self.cost_basis / self.qty if self.qty > 0 else None


@dataclass
class Signal:
    strategy_id: str
    instrument_id: str
    action: Action
    target_weight: Decimal | None  # 전략 슬리브 대비 목표 비중. None이면 현 상태 유지
    rationale: str
    indicators: dict[str, Any] = field(default_factory=dict)
    invalidation: str = ""


@dataclass
class StrategyContext:
    snapshot: Snapshot
    positions: dict[str, PositionView]
    now: datetime

    def pos(self, iid: str) -> PositionView:
        return self.positions.get(iid) or PositionView(Decimal(0), Decimal(0), None)


class Strategy(ABC):
    strategy_id: ClassVar[str]
    version: ClassVar[str]
    title: ClassVar[str]
    hypothesis: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = self.params_model.model_validate(params or {})

    @abstractmethod
    def min_bars(self) -> int: ...

    @abstractmethod
    def decide(self, it: InstrumentSnap, pos: PositionView, share: Decimal, ctx: StrategyContext) -> Signal: ...

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        items = list(ctx.snapshot.items.values())
        n = max(1, len(items))
        share = Decimal(1) / Decimal(n)
        out: list[Signal] = []
        for it in items:
            pos = ctx.pos(it.instrument.instrument_id)
            if len(it.candles) < self.min_bars():
                sig = Signal(self.strategy_id, it.instrument.instrument_id,
                             Action.HOLD if pos.qty > 0 else Action.WAIT, None if pos.qty > 0 else Decimal(0),
                             f"데이터 부족: 완성봉 {len(it.candles)}개 < 필요 {self.min_bars()}개")
            else:
                sig = self.decide(it, pos, share, ctx)
            if sig.action == Action.BUY and not it.ok:
                sig = Signal(sig.strategy_id, sig.instrument_id, Action.HOLD if pos.qty > 0 else Action.WAIT,
                             None if pos.qty > 0 else Decimal(0),
                             "데이터 품질 미달로 신규 매수 보류: " + "; ".join(it.issues), sig.indicators, sig.invalidation)
            out.append(sig)
        return out


REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    REGISTRY[cls.strategy_id] = cls
    return cls


def build_strategies(config: dict[str, Any]) -> list[Strategy]:
    """config: {strategy_id: StrategyToggle-like dict}. 활성화된 등록 전략만 만든다."""
    from aifund.strategies import mean_reversion, trend  # noqa: F401  등록 트리거

    out = []
    for sid, cls in REGISTRY.items():
        toggle = config.get(sid)
        if toggle is None:
            continue
        enabled = toggle.get("enabled", True) if isinstance(toggle, dict) else toggle.enabled
        params = toggle.get("params", {}) if isinstance(toggle, dict) else toggle.params
        if enabled:
            out.append(cls(params))
    return out
