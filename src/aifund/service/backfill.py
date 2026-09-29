"""백테스트용 과거 캔들 수집(공개 시세만 사용)."""

from __future__ import annotations

from aifund.brokers.upbit import UpbitMarketData
from aifund.service.context import AppContext


async def backfill(ctx: AppContext, market: str, days: int) -> int:
    mr = ctx.markets.get(market)
    if mr is None or mr.collector is None or mr.data is None:
        raise RuntimeError(f"{market} 데이터 원천이 없습니다: {mr.data_reason if mr else '비활성'}")
    ms = ctx.settings.markets[market]  # type: ignore[index]
    insts = await mr.collector.refresh_instruments(market, ms.instruments)
    per_day = 1 if ms.candle == "1d" else (1440 // (ms.candle_minutes or 1440))
    total = days * per_day
    n = 0
    for inst in insts:
        if isinstance(mr.data, UpbitMarketData):
            cs = await mr.data.history(inst, ms.candle, total)
        else:
            cs = await mr.data.candles(inst, ms.candle, min(total, 200))
        ctx.market_store.save_candles(cs)
        n += len(cs)
    return n
