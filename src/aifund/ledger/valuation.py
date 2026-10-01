"""장부 평가·노출·손실 기준 추적.

- 평가가: 중간가(없으면 최근가). 가격·환율이 없으면 stale로 표시한다(꾸며내지 않음).
- 노출: 보유 평가액 + 위험 증가 주문의 미체결 예약금(수수료 여유 포함).
- 일 기준 자산(KST 자정 이후 첫 평가)과 고점 자산은 DB에 저장되어 재시작 후에도 유지된다.
- 손실 제한은 신규 위험 증가를 멈추는 운영 기준이며, 급변·장애 시 최대 손실을 보증하지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from aifund.config.settings import RiskSettings
from aifund.core.money import D, ZERO, dstr
from aifund.core.timeutil import Clock, kst_day, to_iso
from aifund.data.fx import FxService
from aifund.db.database import Database, dumps
from aifund.domain.models import Instrument
from aifund.ledger.ledger import Ledger

PriceFn = Callable[[str], Decimal | None]
InstFn = Callable[[str], Instrument | None]

ASSET_CLASS = {"crypto": "코인", "kr_stock": "국내주식", "us_stock": "미국주식"}


@dataclass
class BookValuation:
    book_id: str
    cash: dict[str, Decimal]
    cash_krw: Decimal
    positions_krw: Decimal
    reserved_krw: Decimal
    reserved_buy_krw: Decimal
    equity_krw: Decimal
    realized_krw: Decimal
    unrealized_krw: Decimal
    fees_krw: Decimal
    exposure_krw: Decimal
    by_instrument: dict[str, Decimal] = field(default_factory=dict)
    by_market: dict[str, Decimal] = field(default_factory=dict)
    by_class: dict[str, Decimal] = field(default_factory=dict)
    by_strategy: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    stale: list[str] = field(default_factory=list)
    by_market_strategy: dict[str, dict[str, dict[str, Decimal]]] = field(default_factory=dict)


def value_book(db: Database, ledger: Ledger, book_id: str, price_fn: PriceFn, inst_fn: InstFn, fx: FxService,
               risk: RiskSettings) -> BookValuation:
    stale: list[str] = []
    cash = ledger.cash_by_asset(book_id)

    def krw(amount: Decimal, ccy: str) -> Decimal:
        v, reason = fx.to_krw(amount, ccy, risk.max_fx_age_hours)
        if v is None:
            stale.append(f"{ccy} 환산 불가: {reason}")
            return ZERO
        return v

    cash_krw = sum((krw(v, a) for a, v in cash.items() if v != 0), ZERO)
    by_inst: dict[str, Decimal] = {}
    by_market: dict[str, Decimal] = {}
    by_class: dict[str, Decimal] = {}
    by_strat: dict[str, dict[str, Decimal]] = {}
    by_market_strat: dict[str, dict[str, dict[str, Decimal]]] = {}
    realized = ZERO
    unreal = ZERO
    fees = ZERO
    positions_krw = ZERO
    for p in ledger.positions(book_id, include_closed=True):
        inst = inst_fn(p.instrument_id)
        ccy = inst.quote_ccy if inst else "KRW"
        realized_k = krw(p.realized_pnl, ccy) if p.realized_pnl else ZERO
        fees_k = krw(p.fees, ccy) if p.fees else ZERO
        realized += realized_k
        fees += fees_k
        s = by_strat.setdefault(p.strategy_id, {"value": ZERO, "realized": ZERO, "unrealized": ZERO, "fees": ZERO, "cost": ZERO})
        s["realized"] += realized_k
        s["fees"] += fees_k
        mk = inst.market if inst else p.instrument_id.split(":")[0]
        ms = by_market_strat.setdefault(mk, {}).setdefault(p.strategy_id, {"realized": ZERO, "unrealized": ZERO})
        ms["realized"] += realized_k
        if p.qty == 0:
            continue
        px = price_fn(p.instrument_id)
        if px is None:
            stale.append(f"{p.instrument_id} 가격 없음")
            continue
        value = krw(p.qty * px, ccy)
        cost = krw(p.cost_basis, ccy)
        positions_krw += value
        unreal += value - cost
        ms["unrealized"] += value - cost
        by_inst[p.instrument_id] = by_inst.get(p.instrument_id, ZERO) + value
        mk = inst.market if inst else p.instrument_id.split(":")[0]
        by_market[mk] = by_market.get(mk, ZERO) + value
        cls = ASSET_CLASS.get(mk, mk)
        by_class[cls] = by_class.get(cls, ZERO) + value
        s["value"] += value
        s["unrealized"] += value - cost
        s["cost"] += cost
    reserved = ZERO
    reserved_buy = ZERO
    for r in db.query(
        "SELECT r.kind, r.asset, r.amount_remaining, o.side FROM reservations r JOIN orders o ON o.order_id=r.order_id "
        "WHERE r.book_id=? AND r.status='active'",
        (book_id,),
    ):
        if r["kind"] == "cash":
            v = krw(D(r["amount_remaining"]), r["asset"])
            reserved += v
            if r["side"] == "buy":
                reserved_buy += v
    equity = cash_krw + positions_krw
    return BookValuation(book_id, cash, cash_krw, positions_krw, reserved, reserved_buy, equity, realized, unreal, fees,
                         positions_krw + reserved_buy, by_inst, by_market, by_class, by_strat, stale, by_market_strat)


@dataclass(frozen=True)
class RiskStateView:
    day_kst: str
    day_start_equity: Decimal
    peak_equity: Decimal
    daily_pnl: Decimal
    drawdown_pct: Decimal
    daily_stop_active: bool
    drawdown_stop_active: bool
    daily_stop_at: str | None
    drawdown_stop_at: str | None


class EquityTracker:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def record(self, v: BookValuation, ai_cost_krw: Decimal, risk: RiskSettings) -> RiskStateView:
        now = self.clock.now()
        day = kst_day(now)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO equity_snapshots(book_id, ts, cash_krw, positions_krw, reserved_krw, equity_krw, realized_krw, "
                "unrealized_krw, fees_krw, ai_cost_krw, exposure_json, stale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (v.book_id, to_iso(now), dstr(v.cash_krw), dstr(v.positions_krw), dstr(v.reserved_krw), dstr(v.equity_krw),
                 dstr(v.realized_krw), dstr(v.unrealized_krw), dstr(v.fees_krw), dstr(ai_cost_krw),
                 dumps({"instrument": v.by_instrument, "market": v.by_market, "class": v.by_class}), int(bool(v.stale))),
            )
            return self._update_state(c, v, now, day, risk)

    def _update_state(self, c, v: BookValuation, now: datetime, day: str, risk: RiskSettings) -> RiskStateView:  # type: ignore[no-untyped-def]
        row = c.execute("SELECT * FROM risk_state WHERE book_id=?", (v.book_id,)).fetchone()
        eq = v.equity_krw
        if v.stale:
            # 평가가 불완전하면 기준값을 갱신하지 않는다(낙폭·손실 계산 왜곡 방지)
            if row is None:
                return RiskStateView(day, eq, eq, ZERO, ZERO, False, False, None, None)
            return self._view(row, eq)
        if row is None:
            c.execute(
                "INSERT INTO risk_state(book_id, day_kst, day_start_equity, peak_equity, peak_at, updated_at) VALUES (?,?,?,?,?,?)",
                (v.book_id, day, dstr(eq), dstr(eq), to_iso(now), to_iso(now)),
            )
            row = c.execute("SELECT * FROM risk_state WHERE book_id=?", (v.book_id,)).fetchone()
        if row["day_kst"] != day:
            c.execute("UPDATE risk_state SET day_kst=?, day_start_equity=?, daily_stop_at=NULL, updated_at=? WHERE book_id=?",
                      (day, dstr(eq), to_iso(now), v.book_id))
        if eq > D(row["peak_equity"]):
            c.execute("UPDATE risk_state SET peak_equity=?, peak_at=?, updated_at=? WHERE book_id=?",
                      (dstr(eq), to_iso(now), to_iso(now), v.book_id))
        row = c.execute("SELECT * FROM risk_state WHERE book_id=?", (v.book_id,)).fetchone()
        view = self._view(row, eq)
        if -view.daily_pnl >= risk.daily_loss_stop_krw and not row["daily_stop_at"]:
            c.execute("UPDATE risk_state SET daily_stop_at=?, updated_at=? WHERE book_id=?", (to_iso(now), to_iso(now), v.book_id))
        if view.drawdown_pct >= risk.max_drawdown_stop_pct and not row["drawdown_stop_at"]:
            c.execute("UPDATE risk_state SET drawdown_stop_at=?, updated_at=? WHERE book_id=?", (to_iso(now), to_iso(now), v.book_id))
        row = c.execute("SELECT * FROM risk_state WHERE book_id=?", (v.book_id,)).fetchone()
        return self._view(row, eq)

    @staticmethod
    def _view(row, eq: Decimal) -> RiskStateView:  # type: ignore[no-untyped-def]
        start = D(row["day_start_equity"])
        peak = D(row["peak_equity"])
        dd = (peak - eq) / peak * 100 if peak > 0 else ZERO
        return RiskStateView(row["day_kst"], start, peak, eq - start, max(ZERO, dd), bool(row["daily_stop_at"]),
                             bool(row["drawdown_stop_at"]), row["daily_stop_at"], row["drawdown_stop_at"])

    def state(self, book_id: str) -> RiskStateView | None:
        row = self.db.query_one("SELECT * FROM risk_state WHERE book_id=?", (book_id,))
        if row is None:
            return None
        last = self.db.query_one("SELECT equity_krw FROM equity_snapshots WHERE book_id=? AND stale=0 ORDER BY id DESC LIMIT 1", (book_id,))
        eq = D(last["equity_krw"]) if last else D(row["day_start_equity"])
        view = self._view(row, eq)
        today = kst_day(self.clock.now())
        if view.day_kst != today:
            # 날짜가 바뀌었으면 일손실 정지는 다음 평가 때 해제된다(아직 평가 전이면 유지하지 않음)
            return RiskStateView(view.day_kst, view.day_start_equity, view.peak_equity, view.daily_pnl, view.drawdown_pct,
                                 False, view.drawdown_stop_active, None, view.drawdown_stop_at)
        return view

    def adjust_for_flow(self, book_id: str, delta_krw: Decimal, reason: str) -> None:
        """원금 배정 같은 외부 자금 흐름을 일 기준·고점에 더해 손실 계산에서 제거한다."""
        now = to_iso(self.clock.now())
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM risk_state WHERE book_id=?", (book_id,)).fetchone()
            if row is None:
                return
            c.execute("UPDATE risk_state SET day_start_equity=?, peak_equity=?, updated_at=? WHERE book_id=?",
                      (dstr(D(row["day_start_equity"]) + delta_krw), dstr(D(row["peak_equity"]) + delta_krw), now, book_id))
            c.execute("INSERT INTO control_events(ts, actor, action, scope, detail_json) VALUES (?,?,?,?,?)",
                      (now, "system", "equity_flow_adjust", book_id, dumps({"delta_krw": str(delta_krw), "reason": reason})))

    def reset_drawdown(self, book_id: str, actor: str, reason: str, current_equity: Decimal) -> None:
        now = to_iso(self.clock.now())
        with self.db.tx() as c:
            c.execute("UPDATE risk_state SET drawdown_stop_at=NULL, peak_equity=?, peak_at=?, updated_at=? WHERE book_id=?",
                      (dstr(current_equity), now, now, book_id))
            c.execute("INSERT INTO control_events(ts, actor, action, scope, detail_json) VALUES (?,?,?,?,?)",
                      (now, actor, "reset_drawdown", book_id, dumps({"reason": reason, "new_peak": str(current_equity)})))
