"""규칙 전략 백테스트(개발 구간·평가 구간 분리).

- 봉 t의 종가로 신호를 계산하고 t+1 봉 시가에 슬리피지를 불리하게 적용해 체결(미래정보 방지).
- AI 설정(B/C)은 백테스트하지 않는다: 현재 AI가 과거 사건의 결과를 학습으로 알고 있을 수 있고,
  뉴스 날짜 필터만으로 그 누출을 막을 수 없다. AI 효과는 실시간 사전 기록 비교로만 평가한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from aifund.config.settings import PaperSettings
from aifund.core.money import ZERO, floor_step
from aifund.data.collector import InstrumentSnap, Snapshot
from aifund.domain.models import Action, Candle, Instrument
from aifund.strategies.base import PositionView, Strategy, StrategyContext

LEAKAGE_NOTE = ("과거 구간 백테스트는 규칙 전략만 대상으로 하며, 과최적화·생존편향·체결 가정의 한계가 있습니다. "
                "AI(B/C)는 과거 결과를 이미 알고 있을 수 있어 백테스트로 평가하지 않습니다.")


def side_fee_rate(paper: PaperSettings, market: str) -> Decimal:
    """백테스트 편도 비용률. 내부 모의체결과 같은 시장별 요율을 쓰고, 국내주식 매도세는 매수·매도에 절반씩 나눠 왕복 비용을 맞춘다."""
    if market == "crypto":
        return paper.fee_rate
    if market == "kr_stock":
        return paper.stock_fee_rate + paper.kr_sell_tax_rate / 2
    return paper.us_fee_rate


@dataclass
class SegmentResult:
    start: datetime | None
    end: datetime | None
    return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    trades: int
    fees: Decimal
    buy_hold_return_pct: Decimal | None
    bars: int


def _segment(equity: list[tuple[datetime, Decimal]], trades: list[tuple[datetime, Decimal]], bh: list[tuple[datetime, Decimal]],
             lo: datetime | None, hi: datetime | None) -> SegmentResult:
    eq = [(t, v) for t, v in equity if (lo is None or t > lo) and (hi is None or t <= hi)]
    b = [(t, v) for t, v in bh if (lo is None or t > lo) and (hi is None or t <= hi)]
    tr = [f for t, f in trades if (lo is None or t > lo) and (hi is None or t <= hi)]
    if len(eq) < 2:
        return SegmentResult(lo, hi, None, None, len(tr), sum(tr, ZERO), None, len(eq))
    peak = eq[0][1]
    mdd = ZERO
    for _, v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    ret = (eq[-1][1] / eq[0][1] - 1) * 100
    bhr = (b[-1][1] / b[0][1] - 1) * 100 if len(b) >= 2 and b[0][1] > 0 else None
    return SegmentResult(eq[0][0], eq[-1][0], ret, mdd, len(tr), sum(tr, ZERO), bhr, len(eq))


def run_backtest(strategy: Strategy, candles: dict[str, list[Candle]], instruments: dict[str, Instrument], *,
                 capital: Decimal, fee_rate: Decimal, slippage_bps: Decimal, split_at: datetime | None) -> dict[str, Any]:
    times = sorted({c.close_time for cs in candles.values() for c in cs})
    by_close = {iid: {c.close_time: i for i, c in enumerate(cs)} for iid, cs in candles.items()}
    cash = capital
    qty: dict[str, Decimal] = {iid: ZERO for iid in candles}
    basis: dict[str, Decimal] = {iid: ZERO for iid in candles}
    opened: dict[str, datetime | None] = {iid: None for iid in candles}
    equity: list[tuple[datetime, Decimal]] = []
    trades: list[tuple[datetime, Decimal]] = []
    bh: list[tuple[datetime, Decimal]] = []
    slip = slippage_bps / Decimal(10000)
    pending: list[tuple[str, Action, Decimal | None]] = []
    first_close: dict[str, Decimal] = {}
    for t in times:
        # 1) 직전 봉에서 낸 주문을 이번 봉 시가로 체결
        for iid, action, weight in pending:
            idx = by_close[iid].get(t)
            if idx is None:
                continue
            c = candles[iid][idx]
            inst = instruments[iid]
            if action == Action.SELL and qty[iid] > 0:
                px = c.open * (1 - slip)
                proceeds = qty[iid] * px
                fee = proceeds * fee_rate
                cash += proceeds - fee
                trades.append((t, fee))
                qty[iid] = ZERO
                basis[iid] = ZERO
                opened[iid] = None
            elif action == Action.BUY and weight is not None and qty[iid] == 0:
                px = c.open * (1 + slip)
                budget = min(cash, capital * weight) / (1 + fee_rate)
                q = floor_step(budget / px, inst.qty_step)
                if q > 0 and q * px >= inst.min_notional:
                    fee = q * px * fee_rate
                    cash -= q * px + fee
                    qty[iid] = q
                    basis[iid] = q * px + fee
                    opened[iid] = t
                    trades.append((t, fee))
        pending = []
        # 2) 이번 봉 종가로 평가·신호
        items = {}
        for iid, cs in candles.items():
            idx = by_close[iid].get(t)
            if idx is None:
                continue
            hist = cs[: idx + 1]
            items[iid] = InstrumentSnap(instruments[iid], hist, None)
            first_close.setdefault(iid, hist[-1].close)
        value = cash + sum((qty[i] * items[i].candles[-1].close for i in items), ZERO)
        equity.append((t, value))
        bh_val = sum(((capital / len(candles)) / first_close[i] * items[i].candles[-1].close for i in items if i in first_close), ZERO)
        if len(items) == len(candles):
            bh.append((t, bh_val))
        snap = Snapshot("bt", "backtest", t, "", t, items, "backtest", False, True, "")
        pos = {i: PositionView(qty[i], basis[i], opened[i]) for i in candles}
        for sig in strategy.evaluate(StrategyContext(snap, pos, t)):
            if sig.action in (Action.BUY, Action.SELL):
                pending.append((sig.instrument_id, sig.action, sig.target_weight))
    total = _segment(equity, trades, bh, None, None)
    dev = _segment(equity, trades, bh, None, split_at) if split_at else None
    ev = _segment(equity, trades, bh, split_at, None) if split_at else None
    return {"strategy": strategy.strategy_id, "params": strategy.params.model_dump(mode="json"), "total": total.__dict__,
            "dev": dev.__dict__ if dev else None, "eval": ev.__dict__ if ev else None, "note": LEAKAGE_NOTE,
            "assumptions": {"fee_rate": str(fee_rate), "slippage_bps": str(slippage_bps), "fill": "다음 봉 시가(불리한 슬리피지)"}}
