"""가상 시계 시뮬레이션(offline_demo·재생 데이터 검증용).

실제 서비스 루프와 같은 부품(스냅샷·전략·AI·조정기·위험검사·주문 실행기·모의체결·원장)을 쓰되,
시간을 빠르게 진행시킨다. 실제 거래소 주문은 절대 보내지 않는다(모드가 offline_demo/internal_paper일 때만 허용).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from aifund.control.readiness import reconciler_for
from aifund.core.timeutil import ManualClock, floor_to_interval
from aifund.service.context import AppContext
from aifund.service.cycle import DecisionCycle

log = logging.getLogger(__name__)


async def simulate(ctx: AppContext, hours: int, *, ai: bool = True) -> dict[str, Any]:
    if ctx.mode not in ("offline_demo", "internal_paper"):
        raise PermissionError("시뮬레이션은 offline_demo / internal_paper 모드에서만 허용됩니다")
    if not isinstance(ctx.clock, ManualClock):
        raise TypeError("시뮬레이션에는 ManualClock이 필요합니다")
    clock: ManualClock = ctx.clock
    cycle = DecisionCycle(ctx)
    await ctx.fx.refresh()
    for ex in ctx.all_executors():
        ex.recover_pending()
    summary: dict[str, Any] = {"cycles": 0, "orders": 0, "research": 0, "notes": []}
    for market, mr in ctx.markets.items():
        if mr.collector is not None:
            await mr.collector.refresh_instruments(market, ctx.settings.markets[market].instruments)  # type: ignore[index]
        rec = reconciler_for(ctx, market)
        if rec is not None and mr.operating_executor is not None:
            res = await rec.run("simulation_start")
            ctx.startup_reconciled[mr.operating_executor.account_id] = res.ok or all(m.startswith("상태 불명") for m in res.mismatches)
    for _ in range(hours):
        # 다음 봉 마감 + 지연 시각으로 이동
        now = clock.now()
        nxt = floor_to_interval(now, 60) + timedelta(minutes=60, seconds=25)
        clock.set(nxt)
        for market, mr in ctx.markets.items():
            if mr.data is None:
                continue
            insts = [i for i in (ctx.market_store.instrument(f"{market}:{s}") for s in ctx.settings.markets[market].instruments) if i]  # type: ignore[index]
            ctx.market_store.save_quotes(await mr.data.quotes(insts))
            res = await cycle.run(market)
            summary["cycles"] += 1
            summary["orders"] += len(res.orders)
            if ai and ctx.ai.due_research(market) and market in cycle.last_snapshots:
                snap = cycle.last_snapshots[market]
                rid = await ctx.ai.research(market, snap, cycle.signals_payload(snap), "scheduled")
                if rid:
                    summary["research"] += 1
                    await ctx.ai.review(market, rid)
        # 주문 이후 시세 갱신·체결 확인(주문 시점 이후 시세로만 체결)
        for _k in range(4):
            clock.advance(30)
            for market, mr in ctx.markets.items():
                if mr.data is None:
                    continue
                insts = [i for i in (ctx.market_store.instrument(f"{market}:{s}") for s in ctx.settings.markets[market].instruments) if i]  # type: ignore[index]
                ctx.market_store.save_quotes(await mr.data.quotes(insts))
            for ex in ctx.all_executors():
                await ex.poll()
        for b in ctx.book_ids():
            cycle.record_equity(b)
    for b in ctx.book_ids():
        problems = ctx.ledger.verify(b)
        if problems:
            summary["notes"].append(f"{b}: {problems}")
    return summary
