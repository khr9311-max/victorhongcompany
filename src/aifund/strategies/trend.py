"""전략 봇 A: 이동평균 추세추종(비교 실험용 초기 전략, 검증된 수익 전략 아님)."""

from __future__ import annotations

from decimal import Decimal

from aifund.config.settings import TrendParams
from aifund.data.collector import InstrumentSnap
from aifund.domain.models import Action
from aifund.strategies import indicators as ind
from aifund.strategies.base import PositionView, Signal, Strategy, StrategyContext, register


@register
class TrendSMA(Strategy):
    strategy_id = "trend_sma"
    version = "1.0"
    title = "봇 A · 이동평균 추세"
    hypothesis = "완성봉 종가 기준 단기 이동평균이 장기 이동평균 위에 있고 종가가 장기선 위이면 추세가 이어질 가능성이 있다."
    params_model = TrendParams

    def min_bars(self) -> int:
        return self.params.slow + 2  # type: ignore[attr-defined]

    def decide(self, it: InstrumentSnap, pos: PositionView, share: Decimal, ctx: StrategyContext) -> Signal:
        p: TrendParams = self.params  # type: ignore[assignment]
        closes = it.closes
        fast = ind.sma(closes, p.fast)
        slow = ind.sma(closes, p.slow)
        last = closes[-1]
        assert fast is not None and slow is not None
        buf = float(p.exit_buffer_pct) / 100
        up = fast > slow * (1 + buf) and last > slow
        down = fast < slow * (1 - buf)
        indicators = {"close": last, "sma_fast": round(fast, 4), "sma_slow": round(slow, 4), "fast": p.fast, "slow": p.slow}
        iid = it.instrument.instrument_id
        inval = f"SMA{p.fast}이 SMA{p.slow} 아래로 내려가면 무효"
        if up:
            # 보유 중이면 비중을 다시 맞추지 않는다(가격 변동마다 추가매수 → 회전율 증가 방지)
            if pos.qty > 0:
                return Signal(self.strategy_id, iid, Action.HOLD, None,
                              f"상승 추세 유지: SMA{p.fast} {fast:,.2f} > SMA{p.slow} {slow:,.2f}", indicators, inval)
            return Signal(self.strategy_id, iid, Action.BUY, share,
                          f"상승 추세: SMA{p.fast} {fast:,.2f} > SMA{p.slow} {slow:,.2f}, 종가 {last:,.2f}", indicators, inval)
        if pos.qty > 0 and down:
            return Signal(self.strategy_id, iid, Action.SELL, Decimal(0),
                          f"추세 이탈: SMA{p.fast} {fast:,.2f} < SMA{p.slow} {slow:,.2f}", indicators, "재진입 조건 충족 시")
        if pos.qty > 0:
            return Signal(self.strategy_id, iid, Action.HOLD, None, "추세 판단 경계 구간: 보유 유지", indicators, inval)
        return Signal(self.strategy_id, iid, Action.WAIT, Decimal(0), "추세 조건 미충족: 관망", indicators, "")
