"""포트폴리오 조정기.

전략별 독립 제안(목표 비중) → 전략별 목표 수량 변화 → 같은 종목의 매수·매도 제안을 내부 이전으로 상계 →
남은 순수량만 하나의 거래소 주문으로 만든다. 전략이 실제 계좌를 따로 쓰지 않는다.
관망·현금 유지는 유효한 결정이다. 주문 가격·수량은 최신 실행 가능 시세로 코드가 계산한다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from aifund.core.money import ZERO, floor_step
from aifund.domain.models import Action, Instrument
from aifund.ledger.ledger import PositionRow


@dataclass
class TargetInput:
    strategy_id: str
    instrument_id: str
    action: Action
    target_weight: Decimal | None  # 슬리브 대비, None=유지
    rationale: str
    source: str = "rule"  # rule / ai
    sources: list[str] = field(default_factory=list)
    counterarguments: list[str] = field(default_factory=list)
    invalidation: str = ""
    ai_report_id: str | None = None
    prompt_version: str | None = None
    blocked_reason: str | None = None  # 상위 단계(예: AI 반대, 검증 AI 거절)에서 이미 거절됨


@dataclass
class ProposalRecord:
    strategy_id: str
    instrument_id: str
    action: str
    target_weight: Decimal | None
    target_notional: Decimal | None
    current_notional: Decimal
    rationale: str
    status: str  # adopted / rejected
    decision_reason: str
    source: str
    sources: list[str]
    counterarguments: list[str]
    invalidation: str
    ai_report_id: str | None
    prompt_version: str | None
    delta_qty: Decimal = ZERO


@dataclass
class Cross:
    instrument_id: str
    qty: Decimal
    price: Decimal
    from_strategy: str
    to_strategy: str


@dataclass
class NetOrder:
    instrument_id: str
    side: str
    qty: Decimal
    ref_price: Decimal
    legs: list[tuple[str, Decimal]]
    risk_increasing: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class Plan:
    proposals: list[ProposalRecord]
    crosses: list[Cross]
    orders: list[NetOrder]
    notes: list[str]


def plan(
    *,
    targets: list[TargetInput],
    positions: list[PositionRow],
    sleeve_equity_quote: Callable[[str, str], Decimal | None],  # (strategy, iid) -> 슬리브 자산(해당 종목 통화)
    ref_price: Callable[[str], Decimal | None],
    instruments: dict[str, Instrument],
    rebalance_threshold_quote: Callable[[str], Decimal],
    max_order_quote: Callable[[str], Decimal | None],
    block_buys_reason: Callable[[str], str | None] = lambda iid: None,
) -> Plan:
    pos = {(p.strategy_id, p.instrument_id): p for p in positions}
    records: list[ProposalRecord] = []
    deltas: dict[str, list[tuple[str, Decimal]]] = {}
    notes: list[str] = []
    for t in targets:
        inst = instruments.get(t.instrument_id)
        px = ref_price(t.instrument_id)
        p = pos.get((t.strategy_id, t.instrument_id))
        cur_qty = p.qty if p else ZERO
        cur_val = cur_qty * px if px else ZERO

        def rec(status: str, reason: str, tgt_notional: Decimal | None = None, dq: Decimal = ZERO) -> None:
            records.append(ProposalRecord(t.strategy_id, t.instrument_id, t.action.value, t.target_weight, tgt_notional, cur_val,
                                          t.rationale, status, reason, t.source, t.sources, t.counterarguments, t.invalidation,
                                          t.ai_report_id, t.prompt_version, dq))

        if t.blocked_reason:
            rec("rejected", t.blocked_reason)
            continue
        if inst is None or px is None or px <= 0:
            rec("rejected", "가격 또는 상품 정보 없음")
            continue
        if t.action == Action.SELL:
            if cur_qty <= 0:
                rec("adopted", "보유 없음: 조치 불필요", ZERO)
                continue
            deltas.setdefault(t.instrument_id, []).append((t.strategy_id, -cur_qty))
            rec("adopted", "전량 매도", ZERO, -cur_qty)
            continue
        if t.target_weight is None:
            rec("adopted", "현 상태 유지", None)
            continue
        sleeve = sleeve_equity_quote(t.strategy_id, t.instrument_id)
        if sleeve is None:
            rec("rejected", "슬리브 자산 평가 불가(가격·환율)")
            continue
        target_notional = max(ZERO, sleeve) * t.target_weight
        diff = target_notional - cur_val
        if diff > 0 and t.action in (Action.REDUCE,):
            rec("adopted", "축소 제안이지만 목표가 현재 이상: 유지", target_notional)
            continue
        if abs(diff) < rebalance_threshold_quote(t.instrument_id):
            rec("adopted", "목표와 차이가 재조정 기준 미만: 유지", target_notional)
            continue
        if diff > 0:
            why = block_buys_reason(t.instrument_id)
            if why:
                rec("rejected", why, target_notional)
                continue
        dq = floor_step(abs(diff) / px, inst.qty_step)
        if diff < 0:
            dq = min(dq, cur_qty)
            dq = -dq
        if dq == 0:
            rec("adopted", "수량 단위 미만: 유지", target_notional)
            continue
        deltas.setdefault(t.instrument_id, []).append((t.strategy_id, dq))
        rec("adopted", "매수" if dq > 0 else "축소", target_notional, dq)

    crosses: list[Cross] = []
    orders: list[NetOrder] = []
    for iid, legs in deltas.items():
        inst = instruments[iid]
        px = ref_price(iid)
        assert px is not None
        buys = sorted([[s, q] for s, q in legs if q > 0], key=lambda x: -x[1])
        sells = sorted([[s, -q] for s, q in legs if q < 0], key=lambda x: -x[1])
        # 상계: 매도 전략 → 매수 전략 내부 이전(수수료 없음, 장부 전체 불변)
        bi, si = 0, 0
        while bi < len(buys) and si < len(sells):
            q = floor_step(min(buys[bi][1], sells[si][1]), inst.qty_step)
            if q > 0:
                crosses.append(Cross(iid, q, px, sells[si][0], buys[bi][0]))
                buys[bi][1] -= q
                sells[si][1] -= q
            if buys[bi][1] <= 0:
                bi += 1
            if si < len(sells) and sells[si][1] <= 0:
                si += 1
            if q <= 0:
                break
        rem_buys = [(s, q) for s, q in buys if q > 0]
        rem_sells = [(s, q) for s, q in sells if q > 0]
        if rem_buys:
            total = sum((q for _, q in rem_buys), ZERO)
            cap = max_order_quote(iid)
            if cap is not None and total * px > cap:
                scale = cap / (total * px)
                rem_buys = [(s, floor_step(q * scale, inst.qty_step)) for s, q in rem_buys]
                rem_buys = [(s, q) for s, q in rem_buys if q > 0]
                notes.append(f"{iid}: 1회 주문 한도로 매수 수량 축소(나머지는 다음 주기)")
                total = sum((q for _, q in rem_buys), ZERO)
            if total > 0:
                orders.append(NetOrder(iid, "buy", total, px, rem_buys, True))
        if rem_sells:
            total = sum((q for _, q in rem_sells), ZERO)
            orders.append(NetOrder(iid, "sell", total, px, rem_sells, False))
    return Plan(records, crosses, orders, notes)
