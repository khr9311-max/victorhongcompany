"""중앙 주문 실행기(계좌당 1개, 프로세스 잠금으로 서비스당 1개).

주문 수명주기
  1) 원자적 예약: BEGIN IMMEDIATE 안에서 가용 현금/수량·총노출 한도를 확인하고 주문(pending)·예약·배분을 기록.
  2) submit_attempted_at 기록 후 전송. (crash 시 pending+시도없음=미전송 확정, pending+시도있음=unknown)
  3) 결과: 접수 → submitted / 확정 거부 → rejected(예약 해제) / 응답 유실 → unknown(재주문 금지, 조회로만 해소)
  4) 폴링: 체결은 거래소 체결 ID 또는 누적수량 키로 중복 제거, 누적 감소(순서 역전)는 무시.
  5) 취소: 취소 요청 성공 = cancel_pending. 거래소가 cancel/done을 확인한 뒤에만 canceled/filled 및 예약 해제.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from aifund.brokers.base import BrokerAdapter, BrokerError, OrderLookupHint
from aifund.config.settings import Settings
from aifund.control.flags import Flags, Incidents
from aifund.control.live import LiveNotAuthorized
from aifund.core.ids import client_order_id
from aifund.core.money import D, ZERO, dstr
from aifund.core.timeutil import Clock, parse_iso, to_iso
from aifund.data.fx import FxService
from aifund.data.store import MarketStore
from aifund.db.database import Database, dumps
from aifund.domain.models import (
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    BrokerOrderState,
    Instrument,
    OrderRequest,
    OrderStatus,
    Side,
    SubmitResult,
)
from aifund.ledger.ledger import Ledger

log = logging.getLogger(__name__)

POLLABLE = (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCEL_PENDING)


class ReservationDenied(Exception):
    pass


@dataclass
class OrderIntent:
    intent_id: str
    book_id: str
    account_id: str
    market: str
    instrument: Instrument
    side: Side
    qty: Decimal
    limit_price: Decimal
    ref_price: Decimal
    legs: list[tuple[str, Decimal]]
    risk_increasing: bool
    purpose: str = "rebalance"
    cycle_id: str | None = None
    proposal_ids: list[str] = field(default_factory=list)
    ttl_sec: int = 120
    parent_order_id: str | None = None


def est_fee_rate(inst: Instrument, side: Side, settings: Settings) -> Decimal:
    key = "bid_fee" if side == Side.BUY else "ask_fee"
    v = inst.meta.get(key) if inst.meta else None
    if v:
        return D(v)
    p = settings.execution.paper
    if inst.market == "crypto":
        return p.fee_rate
    if inst.market == "kr_stock":
        return p.stock_fee_rate + (p.kr_sell_tax_rate if side == Side.SELL else ZERO)
    return p.us_fee_rate


class OrderExecutor:
    def __init__(
        self,
        *,
        db: Database,
        ledger: Ledger,
        broker: BrokerAdapter,
        store: MarketStore,
        fx: FxService,
        flags: Flags,
        incidents: Incidents,
        clock: Clock,
        settings_fn: Callable[[], Settings],
        mode: str,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.broker = broker
        self.store = store
        self.fx = fx
        self.flags = flags
        self.incidents = incidents
        self.clock = clock
        self.settings_fn = settings_fn
        self.mode = mode
        self.account_id = broker.account_id
        self._submit_lock = asyncio.Lock()
        self.stopping = False

    # ------------------------------------------------------------------ 조회
    def order(self, order_id: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        return (conn or self.db.conn).execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()

    def open_orders(self) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in OPEN_STATUSES)
        return self.db.query(f"SELECT * FROM orders WHERE account_id=? AND status IN ({marks}) ORDER BY created_at",
                             (self.account_id, *[s.value for s in OPEN_STATUSES]))

    def unknown_count(self, instrument_id: str | None = None) -> int:
        if instrument_id:
            return int(self.db.scalar("SELECT COUNT(*) FROM orders WHERE account_id=? AND status='unknown' AND instrument_id=?",
                                      (self.account_id, instrument_id)))
        return int(self.db.scalar("SELECT COUNT(*) FROM orders WHERE account_id=? AND status='unknown'", (self.account_id,)))

    def pending_buy_instruments(self, book_id: str) -> set[str]:
        rows = self.db.query("SELECT DISTINCT instrument_id FROM orders WHERE book_id=? AND side='buy' AND status IN "
                             "('pending','submitted','partially_filled','cancel_pending','unknown')", (book_id,))
        return {r[0] for r in rows}

    def sellable_qty(self, book_id: str, iid: str, conn: sqlite3.Connection | None = None) -> Decimal:
        c = conn or self.db.conn
        held = self.ledger.book_qty(book_id, iid, c)
        rows = c.execute("SELECT amount_remaining FROM reservations WHERE book_id=? AND kind='qty' AND asset=? AND status='active'",
                         (book_id, iid)).fetchall()
        return held - sum((D(r[0]) for r in rows), ZERO)

    def available_cash(self, book_id: str, ccy: str, conn: sqlite3.Connection | None = None) -> Decimal:
        c = conn or self.db.conn
        cash = self.ledger.cash(book_id, ccy, c)
        rows = c.execute("SELECT amount_remaining FROM reservations WHERE book_id=? AND kind='cash' AND asset=? AND status='active'",
                         (book_id, ccy)).fetchall()
        return cash - sum((D(r[0]) for r in rows), ZERO)

    # ------------------------------------------------------------------ 이벤트
    def _event(self, c: sqlite3.Connection, order_id: str, frm: str | None, to: str, detail: dict) -> None:
        c.execute("INSERT INTO order_events(order_id, ts, from_status, to_status, detail_json) VALUES (?,?,?,?,?)",
                  (order_id, to_iso(self.clock.now()), frm, to, dumps(detail)))

    def _set_status(self, c: sqlite3.Connection, row: sqlite3.Row, to: OrderStatus, detail: dict, **cols: object) -> None:
        frm = row["status"]
        sets = ["status=?", "last_update_at=?"]
        vals: list[object] = [to.value, to_iso(self.clock.now())]
        for k, v in cols.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(row["order_id"])
        c.execute(f"UPDATE orders SET {', '.join(sets)} WHERE order_id=?", vals)
        if frm != to.value or detail:
            self._event(c, row["order_id"], frm, to.value, detail)

    # ------------------------------------------------------------------ 예약·생성
    def _exposure_krw(self, c: sqlite3.Connection, book_id: str) -> Decimal:
        s = self.settings_fn()
        total = ZERO
        for p in self.ledger.positions(book_id, c):
            q = self.store.latest_quote(p.instrument_id)
            px = q.mid if q else None
            inst = self.store.instrument(p.instrument_id)
            if px is None or inst is None:
                raise ReservationDenied(f"노출 평가 불가({p.instrument_id} 가격 없음)")
            v, why = self.fx.to_krw(p.qty * px, inst.quote_ccy, s.risk.max_fx_age_hours, s.risk.fx_haircut_pct)
            if v is None:
                raise ReservationDenied(f"노출 평가 불가: {why}")
            total += v
        for r in c.execute(
            "SELECT r.asset, r.amount_remaining FROM reservations r JOIN orders o ON o.order_id=r.order_id "
            "WHERE r.book_id=? AND r.status='active' AND r.kind='cash' AND o.side='buy'", (book_id,)
        ).fetchall():
            v, why = self.fx.to_krw(D(r["amount_remaining"]), r["asset"], s.risk.max_fx_age_hours, s.risk.fx_haircut_pct)
            if v is None:
                raise ReservationDenied(f"예약금 평가 불가: {why}")
            total += v
        return total

    def reserve_and_create(self, it: OrderIntent) -> str:
        """원자적 예약 + 주문 생성. 같은 intent로 재시도하면 기존 주문 ID를 돌려준다(중복 주문 방지)."""
        s = self.settings_fn()
        inst = it.instrument
        now = self.clock.now()
        fee_rate = est_fee_rate(inst, it.side, s)
        with self.db.tx() as c:
            ex = c.execute("SELECT order_id FROM orders WHERE intent_id=?", (it.intent_id,)).fetchone()
            if ex:
                return str(ex["order_id"])
            if self.stopping:
                raise ReservationDenied("서비스 종료 중: 신규 주문 금지")
            if it.side == Side.BUY:
                amount = it.qty * it.limit_price * (1 + fee_rate) * (1 + s.risk.fee_buffer_pct / 100)
                avail = self.available_cash(it.book_id, inst.quote_ccy, c)
                if amount > avail:
                    raise ReservationDenied(f"가용 현금 부족: 필요 {amount:.2f} > 가용 {avail:.2f} {inst.quote_ccy}")
                if it.risk_increasing:
                    new_krw, why = self.fx.to_krw(amount, inst.quote_ccy, s.risk.max_fx_age_hours, s.risk.fx_haircut_pct)
                    if new_krw is None:
                        raise ReservationDenied(f"원화 환산 불가: {why}")
                    exposure = self._exposure_krw(c, it.book_id)
                    if exposure + new_krw > s.risk.gross_exposure_cap_krw:
                        raise ReservationDenied(
                            f"총노출 한도 초과: 현재 {exposure:,.0f} + 신규 {new_krw:,.0f} > {s.risk.gross_exposure_cap_krw:,.0f}원")
                kind, asset = "cash", inst.quote_ccy
            else:
                amount = it.qty
                sellable = self.sellable_qty(it.book_id, inst.instrument_id, c)
                if amount > sellable:
                    raise ReservationDenied(f"매도 가능 수량 부족: {amount} > {sellable}")
                for sid, q in it.legs:
                    p = self.ledger.position(it.book_id, sid, inst.instrument_id, c)
                    if p is None or p.qty < q:
                        raise ReservationDenied(f"전략 {sid} 보유 부족")
                kind, asset = "qty", inst.instrument_id
            oid = client_order_id()
            ttl = now + timedelta(seconds=it.ttl_sec)
            c.execute(
                "INSERT INTO orders(order_id, book_id, account_id, broker, market, instrument_id, side, order_type, limit_price, qty, "
                "status, risk_increasing, purpose, intent_id, created_at, ttl_expires_at, last_update_at, parent_order_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, it.book_id, self.account_id, self.broker.name, it.market, inst.instrument_id, it.side.value, "limit",
                 dstr(it.limit_price), dstr(it.qty), OrderStatus.PENDING.value, int(it.risk_increasing), it.purpose,
                 it.intent_id, to_iso(now), to_iso(ttl), to_iso(now), it.parent_order_id),
            )
            for sid, q in it.legs:
                c.execute("INSERT INTO order_allocations(order_id, strategy_id, requested_qty) VALUES (?,?,?)", (oid, sid, dstr(q)))
            c.execute(
                "INSERT INTO reservations(reservation_id, order_id, book_id, account_id, kind, asset, amount_initial, amount_remaining, "
                "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("rsv-" + oid, oid, it.book_id, self.account_id, kind, asset, dstr(amount), dstr(amount), "active", to_iso(now), to_iso(now)),
            )
            c.execute("UPDATE intents SET status='ordered', order_id=? WHERE intent_id=?", (oid, it.intent_id))
            self._event(c, oid, None, OrderStatus.PENDING.value,
                        {"reserved": str(amount), "asset": asset, "legs": [[a, str(b)] for a, b in it.legs]})
        return oid

    def _release(self, c: sqlite3.Connection, order_id: str, reason: str) -> None:
        c.execute("UPDATE reservations SET amount_remaining='0', status='released', updated_at=? WHERE order_id=? AND status='active'",
                  (to_iso(self.clock.now()), order_id))

    def _shrink(self, c: sqlite3.Connection, row: sqlite3.Row) -> None:
        qty, filled = D(row["qty"]), D(row["filled_qty"])
        r = c.execute("SELECT * FROM reservations WHERE order_id=?", (row["order_id"],)).fetchone()
        if r is None or r["status"] != "active":
            return
        remaining = D(r["amount_initial"]) * (qty - filled) / qty if qty > 0 else ZERO
        c.execute("UPDATE reservations SET amount_remaining=?, updated_at=? WHERE order_id=?",
                  (dstr(max(ZERO, remaining)), to_iso(self.clock.now()), row["order_id"]))

    # ------------------------------------------------------------------ 전송
    async def submit(self, order_id: str) -> OrderStatus:
        async with self._submit_lock:
            row = self.order(order_id)
            if row is None or row["status"] != OrderStatus.PENDING.value or row["submit_attempted_at"]:
                return OrderStatus(row["status"]) if row else OrderStatus.REJECTED
            inst = self.store.instrument(row["instrument_id"])
            if inst is None:
                with self.db.tx() as c:
                    self._set_status(c, row, OrderStatus.REJECTED, {"reason": "상품 메타데이터 없음"}, last_error="no_instrument")
                    self._release(c, order_id, "no_instrument")
                return OrderStatus.REJECTED
            # 실계좌 주문 가능 금액 확인(계좌 간 자금 이동을 가정하지 않는다)
            if self.broker.is_live_money and row["side"] == "buy":
                need = D(row["qty"]) * D(row["limit_price"])
                orderable: Decimal | None = None
                err = "orderable_insufficient"
                try:
                    orderable = await self.broker.orderable_cash(inst, D(row["limit_price"]))
                except BrokerError as e:
                    err = f"orderable 조회 실패: {e}"
                if orderable is None or orderable < need:
                    with self.db.tx() as c:
                        cur_row = self.order(order_id, c)
                        assert cur_row is not None
                        self._set_status(c, cur_row, OrderStatus.REJECTED,
                                         {"reason": "계좌 주문 가능 금액 부족 또는 조회 실패", "orderable": str(orderable), "need": str(need)},
                                         last_error=err)
                        self._release(c, order_id, "orderable")
                    return OrderStatus.REJECTED
            with self.db.tx() as c:
                row = self.order(order_id, c)
                if row is None or row["submit_attempted_at"]:
                    return OrderStatus(row["status"]) if row else OrderStatus.REJECTED
                c.execute("UPDATE orders SET submit_attempted_at=?, last_update_at=? WHERE order_id=?",
                          (to_iso(self.clock.now()), to_iso(self.clock.now()), order_id))
            req = OrderRequest(order_id, inst, Side(row["side"]), D(row["qty"]), D(row["limit_price"]))
            try:
                result = await self.broker.submit_order(req)
            except LiveNotAuthorized as exc:
                result = SubmitResult.rejected("live_not_authorized", str(exc))
            except BrokerError as exc:
                result = SubmitResult.unknown(exc.code or exc.kind, exc.message)
            except Exception as exc:  # 예측 못한 예외도 '미전송'으로 단정하지 않는다
                result = SubmitResult.unknown("exception", repr(exc)[:200])
            return self._record_submit(order_id, result)

    def _record_submit(self, order_id: str, result: SubmitResult) -> OrderStatus:
        with self.db.tx() as c:
            row = self.order(order_id, c)
            assert row is not None
            if result.outcome == "accepted":
                self._set_status(c, row, OrderStatus.SUBMITTED, {"broker_order_id": result.broker_order_id, **result.meta},
                                 broker_order_id=result.broker_order_id, submitted_at=to_iso(self.clock.now()),
                                 broker_meta_json=dumps(result.meta))
                return OrderStatus.SUBMITTED
            if result.outcome == "rejected":
                self._set_status(c, row, OrderStatus.REJECTED, {"code": result.error_code, "message": result.error_message},
                                 last_error=f"{result.error_code}: {result.error_message}")
                self._release(c, order_id, "rejected")
                if result.error_code in ("jwt_verification", "expired_access_key", "no_authorization_ip", "invalid_query_payload"):
                    self.flags.set(f"auth_error:{self.account_id}", f"인증 오류 {result.error_code}", "system")
                return OrderStatus.REJECTED
            self._set_status(c, row, OrderStatus.UNKNOWN, {"code": result.error_code, "message": result.error_message},
                             unknown_since=to_iso(self.clock.now()), last_error=f"{result.error_code}: {result.error_message}")
        self.incidents.open("order_unknown", f"주문 {order_id} 응답 유실/불명({result.error_code}). 조회로 확인 전까지 위험 증가 주문 차단",
                            severity="critical", account_id=self.account_id)
        return OrderStatus.UNKNOWN

    async def execute(self, it: OrderIntent) -> tuple[str | None, str]:
        try:
            oid = self.reserve_and_create(it)
        except ReservationDenied as exc:
            self.db.execute("UPDATE intents SET status='rejected', risk_reasons_json=? WHERE intent_id=?",
                            (dumps([f"예약 거부: {exc}"]), it.intent_id))
            return None, f"예약 거부: {exc}"
        st = await self.submit(oid)
        return oid, st.value

    # ------------------------------------------------------------------ 상태 반영
    def apply_state(self, order_id: str, st: BrokerOrderState) -> OrderStatus:
        with self.db.tx() as c:
            row = self.order(order_id, c)
            assert row is not None
            cur = OrderStatus(row["status"])
            if cur in TERMINAL_STATUSES:
                return cur
            inst = self.store.instrument(row["instrument_id"])
            if inst is None:
                return cur
            qty = D(row["qty"])
            if st.broker_order_id and not row["broker_order_id"]:
                c.execute("UPDATE orders SET broker_order_id=? WHERE order_id=?", (st.broker_order_id, order_id))
            new_fills = self._collect_fills(c, row, st, inst)
            for key, fq, fp, fee, est, ts in new_fills:
                filled_now = D(self.order(order_id, c)["filled_qty"])  # type: ignore[index]
                if filled_now + fq > qty:
                    self.incidents.open("fill_overflow", f"주문 {order_id} 체결 합계가 주문 수량 초과 → 초과분 무시", severity="critical",
                                        account_id=self.account_id)
                    fq = qty - filled_now
                    if fq <= 0:
                        continue
                cur_ins = c.execute("INSERT OR IGNORE INTO fills(order_id, fill_key, qty, price, fee, fee_estimated, ts, recorded_at, source) "
                                    "VALUES (?,?,?,?,?,?,?,?,?)",
                                    (order_id, key, dstr(fq), dstr(fp), dstr(fee), int(est), to_iso(ts or self.clock.now()),
                                     to_iso(self.clock.now()), self.broker.name))
                if cur_ins.rowcount == 0:
                    continue  # 중복 체결 이벤트
                o = self.order(order_id, c)
                assert o is not None
                self.ledger.apply_fill(c, order=o, fill_qty=fq, fill_price=fp, fee=fee, quote_ccy=inst.quote_ccy,
                                       qty_step=inst.qty_step, ts=ts or self.clock.now())
                c.execute("UPDATE orders SET filled_qty=?, filled_amount=?, fees=?, last_update_at=? WHERE order_id=?",
                          (dstr(D(o["filled_qty"]) + fq), dstr(D(o["filled_amount"]) + fq * fp), dstr(D(o["fees"]) + fee),
                           to_iso(self.clock.now()), order_id))
                self._event(c, order_id, o["status"], o["status"], {"fill": key, "qty": str(fq), "price": str(fp), "fee": str(fee),
                                                                    "fee_estimated": est})
                self._shrink(c, self.order(order_id, c))  # type: ignore[arg-type]
            row = self.order(order_id, c)
            assert row is not None
            filled = D(row["filled_qty"])
            target: OrderStatus
            if st.status == OrderStatus.FILLED or filled >= qty:
                target = OrderStatus.FILLED
            elif st.status in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                target = OrderStatus.CANCELED if st.status == OrderStatus.CANCELED else OrderStatus.REJECTED
                if filled > 0 and target == OrderStatus.REJECTED:
                    target = OrderStatus.CANCELED
            elif cur == OrderStatus.CANCEL_PENDING:
                target = OrderStatus.CANCEL_PENDING  # 취소 요청만 된 상태: 거래소 확인 전까지 유지
            elif filled > 0:
                target = OrderStatus.PARTIALLY_FILLED
            else:
                target = OrderStatus.SUBMITTED
            if target == OrderStatus.FILLED and filled < qty and st.status == OrderStatus.FILLED:
                # 거래소는 완료라는데 체결 기록이 부족 → 누적값으로 보정하지 않고 사고로 남긴다
                self.incidents.open("fill_gap", f"주문 {order_id}: 거래소 완료지만 기록 체결 {filled}/{qty}", severity="critical",
                                    account_id=self.account_id)
            extra = {}
            if cur == OrderStatus.UNKNOWN:
                extra["unknown_since"] = None
            self._set_status(c, row, target, {"broker_state": st.raw_state, "filled": str(filled)} if target != cur else {}, **extra)
            if target in TERMINAL_STATUSES:
                self._release(c, order_id, target.value)
            return target

    def _collect_fills(self, c: sqlite3.Connection, row: sqlite3.Row, st: BrokerOrderState, inst: Instrument
                       ) -> list[tuple[str, Decimal, Decimal, Decimal, bool, datetime | None]]:
        """새 체결 목록 (fill_key, qty, price, fee, fee_estimated, ts)."""
        s = self.settings_fn()
        side = Side(row["side"])
        rate = est_fee_rate(inst, side, s)
        out: list[tuple[str, Decimal, Decimal, Decimal, bool, datetime | None]] = []
        if st.trades is not None:
            known = {r[0] for r in c.execute("SELECT fill_key FROM fills WHERE order_id=?", (row["order_id"],)).fetchall()}
            new = [t for t in st.trades if t.trade_id not in known and t.qty > 0]
            if not new:
                return out
            recorded_fees = D(row["fees"])
            fee_pool = (st.fee_total - recorded_fees) if st.fee_total is not None else None
            new_amt = sum((t.qty * t.price for t in new), ZERO)
            for i, t in enumerate(new):
                if t.fee is not None:
                    fee, est = t.fee, False
                elif fee_pool is not None and fee_pool >= 0 and new_amt > 0:
                    fee = fee_pool * (t.qty * t.price) / new_amt
                    est = False
                else:
                    fee, est = t.qty * t.price * rate, True
                out.append((t.trade_id, t.qty, t.price, fee, est, t.ts))
            return out
        # 누적값만 제공(KIS 등): 누적 증가분으로 체결 합성, 감소는 순서 역전으로 보고 무시
        prev_q = D(row["filled_qty"])
        if st.filled_qty <= prev_q:
            return out
        dq = st.filled_qty - prev_q
        if st.filled_amount is not None and st.filled_amount > 0:
            da = st.filled_amount - D(row["filled_amount"])
            price = da / dq if da > 0 else D(row["limit_price"])
        else:
            price = D(row["limit_price"])
        fee = dq * price * rate
        out.append((f"cum:{st.filled_qty}", dq, price, fee, True, self.clock.now()))
        return out

    # ------------------------------------------------------------------ 폴링·취소·불명 해소
    async def poll(self) -> None:
        rows = self.open_orders()
        now = self.clock.now()
        for row in rows:
            status = OrderStatus(row["status"])
            try:
                if status == OrderStatus.PENDING:
                    continue  # 전송 경로가 처리(재시작 시 recover_pending이 처리)
                if status == OrderStatus.UNKNOWN:
                    await self.resolve_unknown(row["order_id"])
                    continue
                st = await self.broker.get_order(broker_order_id=row["broker_order_id"], client_order_id=row["order_id"])
                if st is None:
                    self.incidents.open("order_missing", f"접수된 주문 {row['order_id']}을 거래소에서 찾을 수 없음", severity="critical",
                                        account_id=self.account_id)
                    continue
                new_status = self.apply_state(row["order_id"], st)
                ttl = parse_iso(row["ttl_expires_at"])
                if new_status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED) and ttl and now >= ttl:
                    await self.request_cancel(row["order_id"], "TTL 만료")
            except BrokerError as exc:
                log.warning("주문 조회 실패 %s: %s", row["order_id"], exc)
                if exc.kind == "auth":
                    self.flags.set(f"auth_error:{self.account_id}", f"주문 조회 인증 오류: {exc.message}", "system")

    async def request_cancel(self, order_id: str, reason: str) -> str:
        row = self.order(order_id)
        if row is None:
            return "없음"
        status = OrderStatus(row["status"])
        if status in TERMINAL_STATUSES:
            return f"이미 종료({status.value})"
        if status == OrderStatus.PENDING and not row["submit_attempted_at"]:
            with self.db.tx() as c:
                self._set_status(c, row, OrderStatus.REJECTED, {"reason": f"전송 전 취소: {reason}"})
                self._release(c, order_id, "cancel_before_send")
            return "전송 전 취소"
        if status == OrderStatus.UNKNOWN:
            await self.resolve_unknown(order_id)
            row = self.order(order_id)
            if row is None or OrderStatus(row["status"]) == OrderStatus.UNKNOWN:
                return "상태 불명: 조회로 확인 후 취소 필요"
        inst = self.store.instrument(row["instrument_id"])
        assert inst is not None
        res = await self.broker.cancel_order(broker_order_id=row["broker_order_id"], client_order_id=order_id, instrument=inst)
        with self.db.tx() as c:
            row = self.order(order_id, c)
            assert row is not None
            if res.accepted and OrderStatus(row["status"]) not in TERMINAL_STATUSES:
                self._set_status(c, row, OrderStatus.CANCEL_PENDING, {"reason": reason}, cancel_requested_at=to_iso(self.clock.now()))
        if res.state is not None and (res.already_final or res.state.status in TERMINAL_STATUSES):
            self.apply_state(order_id, res.state)
        elif not res.accepted:
            # 이미 체결/취소되었을 수 있으므로 재조회해 반영
            try:
                st = await self.broker.get_order(broker_order_id=row["broker_order_id"], client_order_id=order_id)
                if st is not None:
                    self.apply_state(order_id, st)
            except BrokerError:
                pass
            return f"취소 요청 실패: {res.error_code} {res.error_message}"
        return "취소 요청됨(거래소 확인 대기)"

    async def resolve_unknown(self, order_id: str) -> OrderStatus:
        row = self.order(order_id)
        if row is None or row["status"] != OrderStatus.UNKNOWN.value:
            return OrderStatus(row["status"]) if row else OrderStatus.REJECTED
        inst = self.store.instrument(row["instrument_id"])
        assert inst is not None
        linked = {r[0] for r in self.db.query("SELECT broker_order_id FROM orders WHERE account_id=? AND broker_order_id IS NOT NULL",
                                               (self.account_id,))}
        hint = OrderLookupHint(inst, row["side"], D(row["qty"]), D(row["limit_price"]),
                               parse_iso(row["submit_attempted_at"]) or parse_iso(row["created_at"]),  # type: ignore[arg-type]
                               frozenset(linked))
        try:
            st = await self.broker.get_order(broker_order_id=row["broker_order_id"], client_order_id=order_id, hint=hint)
        except BrokerError as exc:
            self.db.execute("UPDATE orders SET lookup_attempts=lookup_attempts+1, last_error=? WHERE order_id=?",
                            (f"조회 실패: {exc}", order_id))
            return OrderStatus.UNKNOWN
        if st is not None:
            with self.db.tx() as c:
                r2 = self.order(order_id, c)
                self._event(c, order_id, "unknown", "unknown", {"resolved": "거래소에서 주문 확인", "broker_order_id": st.broker_order_id})
                if st.broker_order_id and not r2["broker_order_id"]:  # type: ignore[index]
                    c.execute("UPDATE orders SET broker_order_id=?, submitted_at=COALESCE(submitted_at, ?) WHERE order_id=?",
                              (st.broker_order_id, to_iso(self.clock.now()), order_id))
            result = self.apply_state(order_id, st)
            if self.unknown_count() == 0:
                self.incidents.resolve("order_unknown", account_id=self.account_id)
            return result
        # 존재하지 않음: 클라이언트 ID 조회가 가능한 거래소는 짧은 확인 후, 아니면 해소 창 경과 후 '미접수'로 확정
        attempts = int(row["lookup_attempts"]) + 1
        since = parse_iso(row["unknown_since"]) or self.clock.now()
        elapsed = (self.clock.now() - since).total_seconds()
        window = 10 if self.broker.capabilities.client_order_id else self.settings_fn().execution.unknown_resolution_window_sec
        need_attempts = 2 if self.broker.capabilities.client_order_id else 3
        with self.db.tx() as c:
            r2 = self.order(order_id, c)
            assert r2 is not None
            if attempts >= need_attempts and elapsed >= window:
                self._set_status(c, r2, OrderStatus.REJECTED, {"reason": f"거래소 조회 {attempts}회 결과 주문 없음 → 미접수 확정"},
                                 lookup_attempts=attempts, unknown_since=None, last_error="lookup_confirmed_absent")
                self._release(c, order_id, "absent")
                resolved = True
            else:
                c.execute("UPDATE orders SET lookup_attempts=? WHERE order_id=?", (attempts, order_id))
                resolved = False
        if resolved and self.unknown_count() == 0:
            self.incidents.resolve("order_unknown", account_id=self.account_id)
        return OrderStatus.REJECTED if resolved else OrderStatus.UNKNOWN

    def recover_pending(self) -> dict[str, int]:
        """재시작 복구: 전송 시도 없는 pending → 미전송 확정(예약 해제), 시도 있는 pending → unknown."""
        out = {"never_sent": 0, "to_unknown": 0}
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM orders WHERE account_id=? AND status='pending'", (self.account_id,)).fetchall()
            for r in rows:
                if not r["submit_attempted_at"]:
                    self._set_status(c, r, OrderStatus.REJECTED, {"reason": "재시작: 전송 전 중단된 주문(미전송 확정)"},
                                     last_error="never_sent")
                    self._release(c, r["order_id"], "never_sent")
                    out["never_sent"] += 1
                else:
                    self._set_status(c, r, OrderStatus.UNKNOWN, {"reason": "재시작: 전송 중 중단 → 거래소 조회 필요"},
                                     unknown_since=to_iso(self.clock.now()))
                    out["to_unknown"] += 1
        return out

    async def cancel_all(self, reason: str, book_id: str | None = None, instrument_ids: set[str] | None = None) -> list[tuple[str, str]]:
        out = []
        for row in self.open_orders():
            if book_id and row["book_id"] != book_id:
                continue
            if instrument_ids and row["instrument_id"] not in instrument_ids:
                continue
            if row["status"] == OrderStatus.CANCEL_PENDING.value:
                continue
            out.append((row["order_id"], await self.request_cancel(row["order_id"], reason)))
        return out
