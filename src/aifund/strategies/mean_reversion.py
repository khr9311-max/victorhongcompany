"""전략 봇 B: 볼린저밴드·RSI 평균회귀(비교 실험용 초기 전략, 검증된 수익 전략 아님).

손절은 '완성봉 종가 기준 로컬 감시'다. 거래소 측 보호 주문이 아니므로 맥북·네트워크가 꺼지면 동작하지 않는다.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from aifund.config.settings import MeanRevParams
from aifund.data.collector import InstrumentSnap
from aifund.domain.models import Action
from aifund.strategies import indicators as ind
from aifund.strategies.base import PositionView, Signal, Strategy, StrategyContext, register


def _interval_minutes(interval: str) -> int:
    return 1440 if interval == "1d" else int(interval[:-1])


@register
class MeanReversion(Strategy):
    strategy_id = "mean_reversion"
    version = "1.0"
    title = "봇 B · 볼린저/RSI 평균회귀"
    hypothesis = "장기 추세가 크게 무너지지 않은 상태에서 단기 과매도(하단밴드 이탈·RSI 저점)는 평균 방향으로 되돌아올 가능성이 있다."
    params_model = MeanRevParams

    def min_bars(self) -> int:
        p: MeanRevParams = self.params  # type: ignore[assignment]
        return max(p.bb_period, p.rsi_period + 1, p.trend_filter_period) + 1

    def decide(self, it: InstrumentSnap, pos: PositionView, share: Decimal, ctx: StrategyContext) -> Signal:
        p: MeanRevParams = self.params  # type: ignore[assignment]
        closes = it.closes
        bb = ind.bollinger(closes, p.bb_period, float(p.bb_k))
        r = ind.rsi(closes, p.rsi_period)
        trend = ind.sma(closes, p.trend_filter_period)
        last = closes[-1]
        assert bb is not None and r is not None and trend is not None
        lower, mid, upper = bb
        iid = it.instrument.instrument_id
        indicators = {"close": last, "bb_lower": round(lower, 4), "bb_mid": round(mid, 4), "bb_upper": round(upper, 4),
                      "rsi": round(r, 2), "trend_sma": round(trend, 4)}
        if pos.qty > 0:
            avg = float(pos.avg_cost) if pos.avg_cost else None
            if avg and last <= avg * (1 - float(p.stop_loss_pct) / 100):
                return Signal(self.strategy_id, iid, Action.SELL, Decimal(0),
                              f"로컬 손절: 종가 {last:,.2f} ≤ 평균단가 {avg:,.2f}×(1-{p.stop_loss_pct}%)", indicators)
            if pos.opened_at is not None and it.candles:
                held = (it.candles[-1].close_time - pos.opened_at) / timedelta(minutes=_interval_minutes(it.candles[-1].interval))
                indicators["bars_held"] = round(held, 1)
                if held >= p.max_hold_bars:
                    return Signal(self.strategy_id, iid, Action.SELL, Decimal(0), f"보유기간 한도 {p.max_hold_bars}봉 도달", indicators)
            if last >= mid or r >= float(p.rsi_exit):
                return Signal(self.strategy_id, iid, Action.SELL, Decimal(0),
                              f"평균 회귀 완료: 종가 {last:,.2f} ≥ 중심선 {mid:,.2f} 또는 RSI {r:.1f} ≥ {p.rsi_exit}", indicators)
            return Signal(self.strategy_id, iid, Action.HOLD, None, f"회귀 대기: RSI {r:.1f}", indicators,
                          f"종가가 평균단가 대비 -{p.stop_loss_pct}% 이하면 손절")
        floor = trend * (1 - float(p.trend_filter_tolerance_pct) / 100)
        if last < lower and r < float(p.rsi_entry) and last > floor:
            return Signal(self.strategy_id, iid, Action.BUY, share,
                          f"과매도: 종가 {last:,.2f} < 하단 {lower:,.2f}, RSI {r:.1f} < {p.rsi_entry}, 장기선 필터 통과",
                          indicators, f"종가가 평균단가 대비 -{p.stop_loss_pct}% 이하 또는 {p.max_hold_bars}봉 경과")
        reason = "과매도 조건 미충족" if last >= lower or r >= float(p.rsi_entry) else "장기 추세 급락 구간(필터)"
        return Signal(self.strategy_id, iid, Action.WAIT, Decimal(0), f"{reason}: 관망", indicators)
