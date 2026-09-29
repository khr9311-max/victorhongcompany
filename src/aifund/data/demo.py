"""offline_demo 전용 가짜 시세(결정적 난수). 실제 시세·거래로 보이지 않도록 가격대와 이름에 '데모'를 표시한다."""

from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timedelta
from decimal import Decimal

from aifund.brokers.base import MarketData
from aifund.core.money import D
from aifund.core.timeutil import UTC, Clock, SystemClock, floor_to_interval
from aifund.domain.models import Candle, Instrument, Quote
from aifund.markets.rules import UPBIT_KRW_MIN_ORDER, UPBIT_VOLUME_STEP

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
DAY_MIN = 1440


def _seed(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256(":".join(map(str, parts)).encode()).digest()[:8], "big")


class DemoMarketData(MarketData):
    source_name = "DEMO(가짜 데이터)"
    is_demo = True

    def __init__(self, clock: Clock | None = None, seed: int = 7, base_price: Decimal = D("10000")) -> None:
        self.clock = clock or SystemClock()
        self.seed = seed
        self.base = float(base_price)
        self._day_log: dict[str, list[float]] = {}
        self._paths: dict[tuple[str, int], list[float]] = {}

    def _log_start(self, iid: str, day: int) -> float:
        arr = self._day_log.setdefault(iid, [])
        if not arr:
            r0 = random.Random(_seed(self.seed, iid, "base"))
            arr.append(math.log(self.base * (1 + 0.2 * r0.random())))
        while len(arr) <= day:
            d = len(arr) - 1
            r = random.Random(_seed(self.seed, iid, "day", d))
            drift = 0.002 * math.sin(d / 7 + (_seed(self.seed, iid, "ph") % 628) / 100)
            arr.append(arr[-1] + drift + 0.025 * r.gauss(0, 1))
        return arr[day]

    def _path(self, iid: str, day: int) -> list[float]:
        key = (iid, day)
        if key not in self._paths:
            l0, l1 = self._log_start(iid, day), self._log_start(iid, day + 1)
            r = random.Random(_seed(self.seed, iid, "min", day))
            w = [0.0]
            for _ in range(DAY_MIN):
                w.append(w[-1] + 0.0009 * r.gauss(0, 1))
            gap = w[-1] - (l1 - l0)
            # 브라운 브리지: 하루 경로의 끝이 다음 날 시작과 이어진다
            self._paths[key] = [math.exp(l0 + w[m] - (m / DAY_MIN) * gap) for m in range(DAY_MIN + 1)]
            if len(self._paths) > 400:
                self._paths.pop(next(iter(self._paths)))
        return self._paths[key]

    def _price_at(self, iid: str, dt: datetime) -> float:
        minute = int((dt - EPOCH).total_seconds() // 60)
        day, m = divmod(minute, DAY_MIN)
        return self._path(iid, day)[m]

    async def instruments(self, market: str, symbols: list[str]) -> list[Instrument]:
        return [
            Instrument(
                instrument_id=f"{market}:{s}", market=market, symbol=s, exchange="DEMO", name=f"[데모] {s}",
                quote_ccy="KRW", base_asset=s.split("-")[-1], tick_policy="upbit_krw", fixed_tick=None,
                qty_step=UPBIT_VOLUME_STEP, min_notional=UPBIT_KRW_MIN_ORDER, max_notional=None, status="active",
                source="DEMO(가짜 데이터)",
            )
            for s in symbols
        ]

    async def candles(self, instrument: Instrument, interval: str, count: int) -> list[Candle]:
        minutes = DAY_MIN if interval == "1d" else int(interval[:-1])
        now = self.clock.now()
        last_open = floor_to_interval(now, minutes)
        iid = instrument.instrument_id
        out = []
        for k in range(count - 1, -1, -1):
            o_t = last_open - timedelta(minutes=minutes * k)
            c_t = o_t + timedelta(minutes=minutes)
            end_t = min(c_t, now)
            step = max(1, minutes // 15)
            pts = []
            t = o_t
            while t <= end_t:
                pts.append(self._price_at(iid, t))
                t += timedelta(minutes=step)
            pts.append(self._price_at(iid, end_t))
            vol = 1 + 3 * random.Random(_seed(iid, "v", o_t.isoformat())).random()
            out.append(
                Candle(iid, interval, o_t, c_t, D(round(pts[0])), D(round(max(pts))), D(round(min(pts))), D(round(pts[-1])),
                       D(round(vol, 4)), D(round(vol * pts[-1])), "DEMO")
            )
        return out

    async def quotes(self, instruments: list[Instrument]) -> list[Quote]:
        now = self.clock.now()
        out = []
        for i in instruments:
            mid = D(round(self._price_at(i.instrument_id, now)))
            tick = i.tick_for(mid)
            r = random.Random(_seed(i.instrument_id, "q", int(now.timestamp()) // 15))
            bid = (mid // tick) * tick
            ask = bid + tick * (1 + r.randint(0, 2))
            size = D(str(round(0.5 + 5 * r.random(), 6)))
            out.append(Quote(i.instrument_id, bid, ask, size, size, mid, D("5000000000"), now, now, "DEMO"))
        return out
