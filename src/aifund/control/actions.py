"""운영 제어 동작. 세 가지 정지 동작은 서로 분리되어 있고, 각각 설명된 일만 한다.

1) 신규 매수 중지(halt): 위험 증가 주문만 막는다. 기존 주문·보유분·매도(위험 감소)는 유지. 재시작해도 유지.
2) 미체결 취소(cancel-open): 봇이 낸 미체결 주문만 취소 요청한다. 보유분은 건드리지 않는다.
3) 보유분 청산(liquidate): 미리보기 → 확인 문구 입력 후에만 실행. 시장가가 아니라 매수1호가 기준 지정가 매도이며,
   봇 장부 수량만 판다(기존 보유분 제외). 실행 시 해당 시장 신규 매수 중지도 함께 켠다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aifund.control.flags import HALT_GLOBAL, halt_market_key
from aifund.core.ids import new_id
from aifund.core.money import ZERO
from aifund.markets.calendar import session_info
from aifund.portfolio.allocator import NetOrder
from aifund.service.context import OPERATING, AppContext

log = logging.getLogger(__name__)


def halt(ctx: AppContext, scope: str, reason: str, actor: str) -> str:
    key = HALT_GLOBAL if scope == "all" else halt_market_key(scope)
    ctx.flags.set(key, reason or "사용자 요청", actor)
    return f"신규 매수 중지: {scope} (기존 보유·매도·미체결은 유지)"


def resume(ctx: AppContext, scope: str, actor: str, reason: str = "") -> str:
    key = HALT_GLOBAL if scope == "all" else halt_market_key(scope)
    return "재개됨" if ctx.flags.clear(key, actor, reason or "사용자 재개") else "중지 상태가 아니었습니다"


async def cancel_open(ctx: AppContext, market: str | None, actor: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m, mr in ctx.markets.items():
        if market and m != market:
            continue
        if mr.operating_executor is None:
            continue
        ids = {i for i in ctx.settings.markets[m].instruments}  # type: ignore[index]
        iids = {f"{m}:{s}" for s in ids}
        out.extend(await mr.operating_executor.cancel_all(f"사용자 미체결 취소({actor})", book_id=OPERATING, instrument_ids=iids))
    ctx.flags.event(actor, "cancel_open", market or "all", {"results": out})
    return out


@dataclass
class LiquidationPreview:
    market: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confirm_phrase: str = ""


def liquidation_phrase(market: str) -> str:
    return f"청산 {market}"


def preview_liquidation(ctx: AppContext, market: str) -> LiquidationPreview:
    pv = LiquidationPreview(market, confirm_phrase=liquidation_phrase(market))
    s = ctx.settings
    sess = session_info(market, ctx.clock.now())
    if not sess.is_open:
        pv.warnings.append(f"시장 상태: {sess.reason} → 주문이 즉시 체결되지 않을 수 있음")
    agg: dict[str, Decimal] = {}
    for p in ctx.ledger.positions(OPERATING):
        if p.instrument_id.startswith(market + ":"):
            agg[p.instrument_id] = agg.get(p.instrument_id, ZERO) + p.qty
    for iid, qty in agg.items():
        inst = ctx.market_store.instrument(iid)
        q = ctx.market_store.latest_quote(iid)
        bid = q.bid if q else None
        est = qty * bid if bid else None
        row = {"instrument_id": iid, "qty": str(qty), "bid": str(bid) if bid else "-", "est_proceeds": f"{est:,.0f}" if est else "-",
               "note": ""}
        if inst and est is not None and est < inst.min_notional:
            row["note"] = f"최소 주문 금액({inst.min_notional}) 미만 → 거래소에서 매도 불가(잔량 유지)"
        pv.rows.append(row)
    if not pv.rows:
        pv.warnings.append("봇 장부에 청산할 보유분이 없습니다")
    pv.warnings.append(f"지정가 매도(매수1호가 -{s.execution.liquidation_offset_bps}bps, 최대 {s.execution.liquidation_max_reprice}회 재호가). "
                       "시장가 전량 매도가 아니며 체결을 보장하지 않습니다.")
    pv.warnings.append("기존(봇 이전) 보유분은 매도하지 않습니다. 실행 시 해당 시장 신규 매수 중지가 함께 켜집니다.")
    return pv


async def liquidate(ctx: AppContext, market: str, phrase: str, actor: str) -> list[dict[str, Any]]:
    from aifund.service.cycle import CycleResult, DecisionCycle

    if phrase.strip() != liquidation_phrase(market):
        raise PermissionError(f"확인 문구가 일치하지 않습니다: '{liquidation_phrase(market)}'")
    mr = ctx.markets.get(market)
    if mr is None or mr.operating_executor is None:
        raise RuntimeError("이 시장의 주문 실행기가 없습니다")
    halt(ctx, market, f"청산 실행({actor})", actor)
    await mr.operating_executor.cancel_all("청산 전 미체결 취소", book_id=OPERATING,
                                           instrument_ids={f"{market}:{x}" for x in ctx.settings.markets[market].instruments})  # type: ignore[index]
    cycle = DecisionCycle(ctx)
    res = CycleResult(new_id("liq"), market, "liquidation")
    by_iid: dict[str, list[tuple[str, Decimal]]] = {}
    for p in ctx.ledger.positions(OPERATING):
        if p.instrument_id.startswith(market + ":") and p.qty > 0:
            by_iid.setdefault(p.instrument_id, []).append((p.strategy_id, p.qty))
    for iid, legs in by_iid.items():
        total = sum((q for _, q in legs), ZERO)
        q = ctx.market_store.latest_quote(iid)
        ref = q.bid if q and q.bid else None
        if ref is None:
            res.orders.append({"instrument": iid, "status": "호가 없음"})
            continue
        await cycle._execute_net(res.cycle_id, OPERATING, None, mr.operating_executor, NetOrder(iid, "sell", total, ref, legs, False),
                                 [], None, res, purpose="liquidation", offset_bps=ctx.settings.execution.liquidation_offset_bps)
    ctx.flags.set(f"liquidating:{market}", f"청산 진행 중({actor})", actor, value=str(ctx.settings.execution.liquidation_max_reprice))
    ctx.flags.event(actor, "liquidate", market, {"orders": res.orders})
    return res.orders


async def liquidation_followup(ctx: AppContext, market: str) -> None:
    """청산 주문이 TTL로 취소되고 보유분이 남으면 제한 횟수까지 재호가."""
    flag = ctx.flags.get(f"liquidating:{market}")
    if flag is None:
        return
    mr = ctx.markets.get(market)
    if mr is None or mr.operating_executor is None:
        return
    open_liq = ctx.db.scalar("SELECT COUNT(*) FROM orders WHERE book_id=? AND market=? AND purpose='liquidation' AND status IN "
                             "('pending','submitted','partially_filled','cancel_pending','unknown')", (OPERATING, market))
    if open_liq:
        return
    remaining = [p for p in ctx.ledger.positions(OPERATING) if p.instrument_id.startswith(market + ":") and p.qty > 0]
    left = int(flag["value"])
    if not remaining or left <= 0:
        ctx.flags.clear(f"liquidating:{market}", "system", "청산 완료" if not remaining else "재호가 횟수 소진(잔량은 수동 확인)")
        if remaining:
            ctx.incidents.open("liquidation_incomplete", f"{market} 청산 재호가 소진: 잔량 {len(remaining)}종목", market=market)
        return
    await liquidate(ctx, market, liquidation_phrase(market), "system:reprice")
    ctx.flags.set(f"liquidating:{market}", "청산 재호가", "system", value=str(left - 1))
