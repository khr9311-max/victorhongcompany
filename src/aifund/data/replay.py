"""명시적 재생 데이터(internal_paper 검증·테스트용). 저장된 캔들을 시계에 맞춰 재생한다.

재생 시 현재 시각 이후의 봉은 절대 반환하지 않으며(미래정보 방지), 호가는 '현재 진행 중인 봉'의 시가로 만든다.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from aifund.brokers.base import MarketData
from aifund.core.money import D
from aifund.core.timeutil import Clock
from aifund.domain.models import Candle, Instrument, Quote
from aifund.markets.rules import UPBIT_KRW_MIN_ORDER, UPBIT_VOLUME_STEP


class ReplayMarketData(MarketData):
    source_name = "REPLAY(재생 데이터)"

    def __init__(self, candles: dict[str, list[Candle]], clock: Clock, spread_bps: Decimal = D("5"),
                 top_size: Decimal = D("1")) -> None:
        self.data = {k: sorted(v, key=lambda c: c.open_time) for k, v in candles.items()}
        self.clock = clock
        self.spread_bps = spread_bps
        self.top_size = top_size

    @classmethod
    def from_json(cls, path: Path, clock: Clock) -> "ReplayMarketData":
        raw = json.loads(path.read_text(encoding="utf-8"))
        data: dict[str, list[Candle]] = {}
        for iid, rows in raw.items():
            data[iid] = [
                Candle(iid, r["interval"], datetime.fromisoformat(r["open_time"]), datetime.fromisoformat(r["close_time"]),
                       D(r["open"]), D(r["high"]), D(r["low"]), D(r["close"]), D(r["volume"]), None, "REPLAY")
                for r in rows
            ]
        return cls(data, clock)

    async def instruments(self, market: str, symbols: list[str]) -> list[Instrument]:
        return [
            Instrument(f"{market}:{s}", market, s, "REPLAY", f"[재생] {s}", "KRW", s.split("-")[-1], "upbit_krw", None,
                       UPBIT_VOLUME_STEP, UPBIT_KRW_MIN_ORDER, None, "active", "REPLAY")
            for s in symbols
        ]

    async def candles(self, instrument: Instrument, interval: str, count: int) -> list[Candle]:
        now = self.clock.now()
        # 재생에서는 종가가 확정된 봉만 반환한다(진행 중 봉의 최종값 = 미래정보)
        rows = [c for c in self.data.get(instrument.instrument_id, []) if c.close_time <= now and c.interval == interval]
        return rows[-count:]

    def _current(self, iid: str) -> Candle | None:
        now = self.clock.now()
        cur = None
        for c in self.data.get(iid, []):
            if c.open_time <= now:
                cur = c
            else:
                break
        return cur

    async def quotes(self, instruments: list[Instrument]) -> list[Quote]:
        now = self.clock.now()
        out = []
        for i in instruments:
            c = self._current(i.instrument_id)
            if c is None:
                continue
            px = c.open if now < c.close_time else c.close
            tick = i.tick_for(px)
            half = max(tick, (px * self.spread_bps / Decimal(20000) // tick) * tick)
            bid, ask = px - half, px + half
            out.append(Quote(i.instrument_id, bid, ask, self.top_size, self.top_size, px, D("5000000000"), now, now, "REPLAY"))
        return out
