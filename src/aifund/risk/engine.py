"""결정적 위험 검사.

같은 입력이면 항상 같은 결과. AI는 이 규칙이나 한도를 바꿀 수 없다.
위험 감소(보유분 매도) 주문은 가능한 범위에서 유지하고, 위험 증가 주문만 폭넓게 차단한다.
현금·총노출 한도의 최종 판정은 주문 실행기의 원자적 예약 트랜잭션에서 한 번 더 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from aifund.config.settings import RiskSettings
from aifund.core.money import is_multiple
from aifund.domain.models import Instrument, Quote, Side
from aifund.ledger.valuation import RiskStateView


@dataclass
class RiskInputs:
    mode: str
    market: str
    book_kind: str
    account_id: str
    instrument: Instrument
    side: Side
    qty: Decimal
    limit_price: Decimal
    notional_krw: Decimal | None
    risk_increasing: bool
    quote: Quote | None
    now: datetime
    snapshot_issues: list[str] = field(default_factory=list)
    session_open: bool = True
    session_reason: str = ""
    halted_reason: str | None = None
    account_blocks: list[str] = field(default_factory=list)
    startup_reconciled: bool = True
    unknown_orders_account: int = 0
    unknown_orders_instrument: int = 0
    risk_state: RiskStateView | None = None
    ai_hold_reason: str | None = None
    clock_skew_sec: float | None = None
    fx_ok: bool = True
    fx_reason: str = ""
    broker_auth_ok: bool | None = None
    held_instruments: set[str] = field(default_factory=set)
    pending_buy_instruments: set[str] = field(default_factory=set)
    sellable_qty: Decimal = Decimal(0)
    live_authorized: tuple[bool, str] | None = None
    purpose: str = "rebalance"


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str]
    checks: list[tuple[str, bool, str]]


def evaluate(inp: RiskInputs, s: RiskSettings) -> RiskDecision:
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    inst = inp.instrument
    # ---- 모든 주문 공통 ----
    chk("재시작 후 대사 완료", inp.startup_reconciled, "" if inp.startup_reconciled else "대사 전 신규 주문 금지")
    chk("계좌 차단 없음", not inp.account_blocks, "; ".join(inp.account_blocks))
    if inp.mode == "live" and inp.book_kind == "operating":
        ok, why = inp.live_authorized or (False, "LIVE 권한 확인 불가")
        chk("LIVE 활성 범위", ok, why)
    q = inp.quote
    if q is None or (inp.side == Side.BUY and q.ask is None) or (inp.side == Side.SELL and q.bid is None):
        chk("실행 가능 호가", False, "호가 없음")
    else:
        age = (inp.now - q.fetched_at).total_seconds()
        chk("호가 신선도", age <= s.max_quote_age_sec, f"{age:.0f}초 (한도 {s.max_quote_age_sec}초)")
    chk("수량 > 0", inp.qty > 0, str(inp.qty))
    chk("수량 단위", inp.qty > 0 and is_multiple(inp.qty, inst.qty_step), f"{inp.qty} / 단위 {inst.qty_step}")
    tick = inst.tick_for(inp.limit_price) if inp.limit_price > 0 else None
    chk("호가 단위", tick is not None and is_multiple(inp.limit_price, tick), f"{inp.limit_price} / 단위 {tick}")
    notional_q = inp.qty * inp.limit_price
    chk("최소 주문 금액", notional_q >= inst.min_notional, f"{notional_q} ≥ {inst.min_notional} {inst.quote_ccy}")
    if inst.max_notional is not None:
        chk("최대 주문 금액(거래소)", notional_q <= inst.max_notional, f"{notional_q} ≤ {inst.max_notional}")

    if inp.side == Side.SELL:
        chk("봇 보유분 이내 매도", inp.qty <= inp.sellable_qty, f"{inp.qty} ≤ 매도 가능 {inp.sellable_qty}")
        chk("같은 종목 상태불명 주문 없음", inp.unknown_orders_instrument == 0, f"{inp.unknown_orders_instrument}건")

    if inp.risk_increasing:
        chk("상태불명 주문 없음(계좌)", inp.unknown_orders_account == 0, f"{inp.unknown_orders_account}건 해소 전 위험 증가 금지")
        chk("신규 매수 중지 아님", inp.halted_reason is None, inp.halted_reason or "")
        rs = inp.risk_state
        if rs is not None:
            chk("일손실 한도", not rs.daily_stop_active, f"일손익 {rs.daily_pnl:,.0f}원 / 한도 -{s.daily_loss_stop_krw:,.0f}원")
            chk("최대 낙폭 한도", not rs.drawdown_stop_active, f"낙폭 {rs.drawdown_pct:.2f}% / 한도 {s.max_drawdown_stop_pct}%")
        chk("AI 상태", inp.ai_hold_reason is None, inp.ai_hold_reason or "")
        chk("데이터 품질", not inp.snapshot_issues, "; ".join(inp.snapshot_issues))
        chk("장 운영 중", inp.session_open, inp.session_reason)
        chk("상품 상태", inst.status == "active", inst.status)
        if q is not None and q.spread_pct is not None:
            chk("스프레드", q.spread_pct <= s.max_spread_pct, f"{q.spread_pct:.3f}% ≤ {s.max_spread_pct}%")
        if inp.clock_skew_sec is not None:
            chk("시계 오차", abs(inp.clock_skew_sec) <= s.max_clock_skew_sec, f"{inp.clock_skew_sec:+.1f}초")
        if inst.quote_ccy != "KRW":
            chk("환율 신선도", inp.fx_ok, inp.fx_reason)
        if inp.broker_auth_ok is not None:
            chk("계좌 인증", inp.broker_auth_ok, "" if inp.broker_auth_ok else "최근 인증/계좌 확인 실패")
        if inp.notional_krw is None:
            chk("원화 환산 금액", False, "환산 불가")
        else:
            chk("1회 주문 한도", inp.notional_krw <= s.max_order_notional_krw,
                f"{inp.notional_krw:,.0f}원 ≤ {s.max_order_notional_krw:,.0f}원")
        new_name = inst.instrument_id not in inp.held_instruments and inst.instrument_id not in inp.pending_buy_instruments
        count = len(inp.held_instruments | inp.pending_buy_instruments)
        if new_name:
            chk("최대 보유 종목 수", count < s.max_open_positions, f"{count}/{s.max_open_positions}")

    reasons = [f"{n}: {d}" if d else n for n, ok, d in checks if not ok]
    return RiskDecision(not reasons, reasons, checks)
