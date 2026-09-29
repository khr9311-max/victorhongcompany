"""내부 모의체결 브로커(internal_paper / offline_demo / 가상 비교 장부).

실거래와 다르다:
- 주문 '이후'에 수집된 호가로만 체결을 판정한다(신호 계산에 쓴 종가로 체결됐다고 가정하지 않음).
- 매수는 매도1호가 이상으로 불리하게(슬리피지 bps 가산), 매도는 매수1호가 이하로 불리하게 체결한다.
- 1호가 잔량 × max_fill_fraction 까지만 한 번에 체결 → 부분체결이 생길 수 있다.
- 수수료(시장별 요율)와 국내주식 매도세를 차감한다.
- 실제 호가창 깊이·대기열 위치·거래소 장애는 모사하지 않는다.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from decimal import Decimal

from aifund.brokers.base import BrokerAdapter, BrokerCapabilities, OrderLookupHint
from aifund.config.settings import PaperSettings
from aifund.core.money import D, ZERO, dstr, floor_step, is_multiple
from aifund.core.timeutil import Clock, parse_iso, to_iso
from aifund.data.store import MarketStore
from aifund.db.database import Database
from aifund.domain.models import (
    AccountCheck,
    Balance,
    BrokerOrderState,
    CancelResult,
    Instrument,
    OrderRequest,
    OrderStatus,
    Side,
    SubmitResult,
    TradeFill,
)


def paper_fee_rate(settings: PaperSettings, inst: Instrument, side: str) -> Decimal:
    if inst.market == "crypto":
        return settings.fee_rate
    if inst.market == "kr_stock":
        return settings.stock_fee_rate + (settings.kr_sell_tax_rate if side == "sell" else ZERO)
    return settings.us_fee_rate


class PaperBroker(BrokerAdapter):
    name = "paper"
    is_live_money = False

    def __init__(self, db: Database, account_id: str, store: MarketStore, clock: Clock, settings: PaperSettings) -> None:
        super().__init__(account_id)
        self.db = db
        self.store = store
        self.clock = clock
        self.s = settings

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            quotes=True, candles=True, instrument_meta=True, balances=True, orderable_cash=True, place_order=True,
            order_test=True, cancel_order=True, client_order_id=True, open_orders=True, per_trade_fills=True,
            exchange_fees_reported=True, server_side_stop=False, sandbox=False,
            notes=("내부 모의체결: 실거래 체결과 다를 수 있음(호가 깊이·대기열 미반영)",),
        )

    # --- 자금 ---
    def fund(self, asset: str, amount: Decimal) -> None:
        """모의 계좌 최초 입금(이미 있으면 무시)."""
        self.db.execute(
            "INSERT OR IGNORE INTO paper_balances(account_id, asset, free, locked) VALUES (?,?,?,?)",
            (self.account_id, asset, dstr(amount), "0"),
        )

    def _bal(self, c: sqlite3.Connection, asset: str) -> tuple[Decimal, Decimal]:
        r = c.execute("SELECT free, locked FROM paper_balances WHERE account_id=? AND asset=?", (self.account_id, asset)).fetchone()
        return (D(r["free"]), D(r["locked"])) if r else (ZERO, ZERO)

    def _set(self, c: sqlite3.Connection, asset: str, free: Decimal, locked: Decimal) -> None:
        c.execute(
            "INSERT INTO paper_balances(account_id, asset, free, locked) VALUES (?,?,?,?) "
            "ON CONFLICT(account_id, asset) DO UPDATE SET free=excluded.free, locked=excluded.locked",
            (self.account_id, asset, dstr(free), dstr(locked)),
        )

    async def check_account(self) -> AccountCheck:
        return AccountCheck(ok=True, auth_ok=True, detail={"paper": True})

    async def balances(self) -> dict[str, Balance]:
        rows = self.db.query("SELECT asset, free, locked FROM paper_balances WHERE account_id=?", (self.account_id,))
        return {r["asset"]: Balance(r["asset"], D(r["free"]), D(r["locked"])) for r in rows}

    async def orderable_cash(self, instrument: Instrument, price: Decimal) -> Decimal:
        free, _ = self._bal(self.db.conn, instrument.quote_ccy)
        return free

    # --- 주문 ---
    async def submit_order(self, req: OrderRequest) -> SubmitResult:
        inst = req.instrument
        if req.qty <= 0 or not is_multiple(req.qty, inst.qty_step):
            return SubmitResult.rejected("invalid_volume", f"수량 단위 오류 {req.qty} (단위 {inst.qty_step})")
        tick = inst.tick_for(req.limit_price)
        if not is_multiple(req.limit_price, tick):
            return SubmitResult.rejected("invalid_price", f"호가 단위 오류 {req.limit_price} (단위 {tick})")
        if req.qty * req.limit_price < inst.min_notional:
            return SubmitResult.rejected("under_min_total", f"최소 주문 금액 {inst.min_notional} 미만")
        rate = paper_fee_rate(self.s, inst, req.side.value)
        with self.db.tx() as c:
            ex = c.execute("SELECT broker_order_id FROM paper_orders WHERE account_id=? AND client_order_id=?",
                           (self.account_id, req.client_order_id)).fetchone()
            if ex:
                return SubmitResult.unknown("duplicated_identifier", "이미 등록된 주문 ID")
            if req.side == Side.BUY:
                need = req.qty * req.limit_price * (1 + rate)
                free, locked = self._bal(c, inst.quote_ccy)
                if free < need:
                    return SubmitResult.rejected("insufficient_funds_bid", f"모의 잔고 부족 {free} < {need}")
                self._set(c, inst.quote_ccy, free - need, locked + need)
            else:
                free, locked = self._bal(c, inst.instrument_id)
                if free < req.qty:
                    return SubmitResult.rejected("insufficient_funds_ask", f"모의 보유수량 부족 {free} < {req.qty}")
                self._set(c, inst.instrument_id, free - req.qty, locked + req.qty)
            last = self.store.latest_quote(inst.instrument_id)
            boid = "paper-" + uuid.uuid4().hex[:16]
            c.execute(
                "INSERT INTO paper_orders(broker_order_id, account_id, client_order_id, instrument_id, side, limit_price, qty, "
                "filled_qty, filled_amount, fees, status, created_at, last_match_quote_id, trades_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (boid, self.account_id, req.client_order_id, inst.instrument_id, req.side.value, dstr(req.limit_price),
                 dstr(req.qty), "0", "0", "0", "wait", to_iso(self.clock.now()), last.quote_id if last else 0, "[]"),
            )
        return SubmitResult.accepted(boid)

    async def test_order(self, req: OrderRequest) -> SubmitResult:
        inst = req.instrument
        if not is_multiple(req.limit_price, inst.tick_for(req.limit_price)):
            return SubmitResult.rejected("invalid_price", "호가 단위 오류")
        return SubmitResult.accepted("paper-test")

    def _row(self, c: sqlite3.Connection, broker_order_id: str | None, client_order_id: str) -> sqlite3.Row | None:
        if broker_order_id:
            return c.execute("SELECT * FROM paper_orders WHERE broker_order_id=? AND account_id=?",
                             (broker_order_id, self.account_id)).fetchone()
        return c.execute("SELECT * FROM paper_orders WHERE client_order_id=? AND account_id=?",
                         (client_order_id, self.account_id)).fetchone()

    def _state(self, r: sqlite3.Row) -> BrokerOrderState:
        st = r["status"]
        filled = D(r["filled_qty"])
        status = {"done": OrderStatus.FILLED, "cancel": OrderStatus.CANCELED}.get(
            st, OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.SUBMITTED)
        trades = [TradeFill(t["id"], D(t["qty"]), D(t["price"]), D(t["fee"]), parse_iso(t["ts"])) for t in json.loads(r["trades_json"])]
        return BrokerOrderState(r["broker_order_id"], r["client_order_id"], status, filled, D(r["filled_amount"]),
                                D(r["fees"]), trades, st, r["instrument_id"], Side(r["side"]), D(r["qty"]), D(r["limit_price"]))

    def _match(self, c: sqlite3.Connection, r: sqlite3.Row) -> sqlite3.Row:
        if r["status"] != "wait":
            return r
        inst = self.store.instrument(r["instrument_id"])
        created = parse_iso(r["created_at"])
        q = self.store.quote_after(r["instrument_id"], int(r["last_match_quote_id"] or 0), created)  # type: ignore[arg-type]
        if inst is None or q is None:
            return r
        side = r["side"]
        limit = D(r["limit_price"])
        qty = D(r["qty"])
        filled = D(r["filled_qty"])
        remaining = qty - filled
        slip = self.s.slippage_bps / Decimal(10000)
        if side == "buy":
            if q.ask is None or q.ask > limit:
                c.execute("UPDATE paper_orders SET last_match_quote_id=? WHERE broker_order_id=?", (q.quote_id, r["broker_order_id"]))
                return c.execute("SELECT * FROM paper_orders WHERE broker_order_id=?", (r["broker_order_id"],)).fetchone()
            price = min(limit, q.ask * (1 + slip))
            size = q.ask_size
        else:
            if q.bid is None or q.bid < limit:
                c.execute("UPDATE paper_orders SET last_match_quote_id=? WHERE broker_order_id=?", (q.quote_id, r["broker_order_id"]))
                return c.execute("SELECT * FROM paper_orders WHERE broker_order_id=?", (r["broker_order_id"],)).fetchone()
            price = max(limit, q.bid * (1 - slip))
            size = q.bid_size
        cap = remaining if size is None else floor_step(size * self.s.max_fill_fraction, inst.qty_step)
        fill = min(remaining, max(cap, inst.qty_step))
        rate = paper_fee_rate(self.s, inst, side)
        fee = fill * price * rate
        if side == "buy":
            free, locked = self._bal(c, inst.quote_ccy)
            release = fill * limit * (1 + rate)
            self._set(c, inst.quote_ccy, free + release - (fill * price + fee), locked - release)
            bf, bl = self._bal(c, inst.instrument_id)
            self._set(c, inst.instrument_id, bf + fill, bl)
        else:
            bf, bl = self._bal(c, inst.instrument_id)
            self._set(c, inst.instrument_id, bf, bl - fill)
            free, locked = self._bal(c, inst.quote_ccy)
            self._set(c, inst.quote_ccy, free + fill * price - fee, locked)
        trades = json.loads(r["trades_json"])
        trades.append({"id": f"{r['broker_order_id']}-t{len(trades) + 1}", "qty": dstr(fill), "price": dstr(price),
                       "fee": dstr(fee), "ts": to_iso(self.clock.now())})
        new_filled = filled + fill
        status = "done" if new_filled >= qty else "wait"
        c.execute(
            "UPDATE paper_orders SET filled_qty=?, filled_amount=?, fees=?, status=?, last_match_quote_id=?, trades_json=? "
            "WHERE broker_order_id=?",
            (dstr(new_filled), dstr(D(r["filled_amount"]) + fill * price), dstr(D(r["fees"]) + fee), status, q.quote_id,
             json.dumps(trades), r["broker_order_id"]),
        )
        return c.execute("SELECT * FROM paper_orders WHERE broker_order_id=?", (r["broker_order_id"],)).fetchone()

    async def get_order(self, *, broker_order_id: str | None, client_order_id: str,
                        hint: OrderLookupHint | None = None) -> BrokerOrderState | None:
        with self.db.tx() as c:
            r = self._row(c, broker_order_id, client_order_id)
            if r is None:
                return None
            r = self._match(c, r)
            return self._state(r)

    async def cancel_order(self, *, broker_order_id: str | None, client_order_id: str, instrument: Instrument) -> CancelResult:
        with self.db.tx() as c:
            r = self._row(c, broker_order_id, client_order_id)
            if r is None:
                return CancelResult(accepted=False, error_code="order_not_found", error_message="모의 주문 없음")
            if r["status"] != "wait":
                return CancelResult(accepted=False, already_final=True, state=self._state(r))
            inst = self.store.instrument(r["instrument_id"])
            remaining = D(r["qty"]) - D(r["filled_qty"])
            if inst is not None:
                if r["side"] == "buy":
                    rate = paper_fee_rate(self.s, inst, "buy")
                    free, locked = self._bal(c, inst.quote_ccy)
                    rel = remaining * D(r["limit_price"]) * (1 + rate)
                    self._set(c, inst.quote_ccy, free + rel, locked - rel)
                else:
                    bf, bl = self._bal(c, inst.instrument_id)
                    self._set(c, inst.instrument_id, bf + remaining, bl - remaining)
            c.execute("UPDATE paper_orders SET status='cancel' WHERE broker_order_id=?", (r["broker_order_id"],))
            r2 = c.execute("SELECT * FROM paper_orders WHERE broker_order_id=?", (r["broker_order_id"],)).fetchone()
            return CancelResult(accepted=True, state=self._state(r2))

    async def open_orders(self, instruments: list[Instrument]) -> list[BrokerOrderState]:
        ids = {i.instrument_id for i in instruments}
        rows = self.db.query("SELECT * FROM paper_orders WHERE account_id=? AND status='wait'", (self.account_id,))
        return [self._state(r) for r in rows if not ids or r["instrument_id"] in ids]
