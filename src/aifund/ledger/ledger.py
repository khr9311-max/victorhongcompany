"""원장.

- 장부(book)마다 현금(통화별)·수량은 ledger_entries의 합이다. positions는 여기서 파생된 상태이며 verify()로 재계산 검증한다.
- 체결은 주문의 전략별 요청 수량(order_allocations) 비율로 배분한다. 전략 합계 = 장부 실제 수량/현금(불변식).
- 같은 종목의 상충 제안은 내부 이전(internal_transfer)으로 상계한다: 장부 전체 현금·수량은 변하지 않는 제로섬.
- 원가법: 평균원가. 매수 수수료는 원가에 포함, 매도 수수료는 실현손익에서 차감.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from aifund.core.money import D, ZERO, dstr, floor_step
from aifund.core.timeutil import Clock, parse_iso, to_iso
from aifund.db.database import Database

BOOK_STRATEGY = "_book"
CASH_ASSETS = ("KRW", "USD")


class LedgerError(Exception):
    pass


@dataclass(frozen=True)
class PositionRow:
    book_id: str
    strategy_id: str
    instrument_id: str
    qty: Decimal
    cost_basis: Decimal
    realized_pnl: Decimal
    fees: Decimal
    opened_at: datetime | None

    @property
    def avg_cost(self) -> Decimal | None:
        return self.cost_basis / self.qty if self.qty > 0 else None


@dataclass(frozen=True)
class LegAlloc:
    strategy_id: str
    qty: Decimal
    fee: Decimal


def _pos(r: sqlite3.Row) -> PositionRow:
    return PositionRow(r["book_id"], r["strategy_id"], r["instrument_id"], D(r["qty"]), D(r["cost_basis"]),
                       D(r["realized_pnl"]), D(r["fees"]), parse_iso(r["opened_at"]))


class Ledger:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    # ---------- 장부 ----------
    def create_book(self, book_id: str, *, kind: str, setting: str, principal_krw: Decimal, virtual: bool,
                    account_id: str, description: str) -> bool:
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM books WHERE book_id=?", (book_id,)).fetchone():
                return False
            now = to_iso(self.clock.now())
            c.execute(
                "INSERT INTO books(book_id, kind, setting, principal_krw, virtual, account_id, created_at, description) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (book_id, kind, setting, dstr(principal_krw), int(virtual), account_id, now, description),
            )
            c.execute(
                "INSERT INTO ledger_entries(book_id, ts, kind, strategy_id, asset, delta, ref_type, note) VALUES (?,?,?,?,?,?,?,?)",
                (book_id, now, "principal", BOOK_STRATEGY, "KRW", dstr(principal_krw), "book",
                 "가상 원금(실사용 예산과 합산 금지)" if virtual else "운용 원금 배정"),
            )
            return True

    def books(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM books ORDER BY kind, book_id")

    def book(self, book_id: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM books WHERE book_id=?", (book_id,))

    # ---------- 조회 ----------
    def _conn(self, conn: sqlite3.Connection | None) -> sqlite3.Connection:
        return conn or self.db.conn

    def cash(self, book_id: str, asset: str = "KRW", conn: sqlite3.Connection | None = None) -> Decimal:
        rows = self._conn(conn).execute("SELECT delta FROM ledger_entries WHERE book_id=? AND asset=?", (book_id, asset)).fetchall()
        return sum((D(r[0]) for r in rows), ZERO)

    def cash_by_asset(self, book_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Decimal]:
        return {a: self.cash(book_id, a, conn) for a in CASH_ASSETS}

    def principal(self, book_id: str) -> Decimal:
        rows = self.db.query("SELECT delta FROM ledger_entries WHERE book_id=? AND kind='principal'", (book_id,))
        return sum((D(r[0]) for r in rows), ZERO)

    def positions(self, book_id: str, conn: sqlite3.Connection | None = None, include_closed: bool = False) -> list[PositionRow]:
        rows = self._conn(conn).execute("SELECT * FROM positions WHERE book_id=? ORDER BY instrument_id, strategy_id", (book_id,)).fetchall()
        out = [_pos(r) for r in rows]
        return out if include_closed else [p for p in out if p.qty != 0]

    def position(self, book_id: str, strategy_id: str, iid: str, conn: sqlite3.Connection | None = None) -> PositionRow | None:
        r = self._conn(conn).execute(
            "SELECT * FROM positions WHERE book_id=? AND strategy_id=? AND instrument_id=?", (book_id, strategy_id, iid)
        ).fetchone()
        return None if r is None else _pos(r)

    def book_qty(self, book_id: str, iid: str, conn: sqlite3.Connection | None = None) -> Decimal:
        rows = self._conn(conn).execute("SELECT qty FROM positions WHERE book_id=? AND instrument_id=?", (book_id, iid)).fetchall()
        return sum((D(r[0]) for r in rows), ZERO)

    def held_instruments(self, book_id: str, conn: sqlite3.Connection | None = None) -> set[str]:
        out: dict[str, Decimal] = {}
        for p in self.positions(book_id, conn):
            out[p.instrument_id] = out.get(p.instrument_id, ZERO) + p.qty
        return {k for k, v in out.items() if v > 0}

    # ---------- 쓰기(반드시 호출자의 트랜잭션 안에서) ----------
    def _entry(self, c: sqlite3.Connection, book_id: str, ts: str, kind: str, strategy: str, asset: str, delta: Decimal,
               price: Decimal | None, ref_type: str, ref_id: str, note: str | None = None) -> None:
        c.execute(
            "INSERT INTO ledger_entries(book_id, ts, kind, strategy_id, asset, delta, price, ref_type, ref_id, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (book_id, ts, kind, strategy, asset, dstr(delta), dstr(price), ref_type, ref_id, note),
        )

    def _upsert_pos(self, c: sqlite3.Connection, book_id: str, strategy: str, iid: str, qty: Decimal, basis: Decimal,
                    realized: Decimal, fees: Decimal, opened_at: str | None, ts: str) -> None:
        c.execute(
            "INSERT INTO positions(book_id, strategy_id, instrument_id, qty, cost_basis, realized_pnl, fees, opened_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(book_id, strategy_id, instrument_id) DO UPDATE SET qty=excluded.qty, "
            "cost_basis=excluded.cost_basis, realized_pnl=excluded.realized_pnl, fees=excluded.fees, opened_at=excluded.opened_at, "
            "updated_at=excluded.updated_at",
            (book_id, strategy, iid, dstr(qty), dstr(basis), dstr(realized), dstr(fees), opened_at, ts),
        )

    def _buy(self, c: sqlite3.Connection, book_id: str, strategy: str, iid: str, qty: Decimal, price: Decimal, fee: Decimal,
             ccy: str, ts: str, ref_type: str, ref_id: str, kind: str = "fill") -> None:
        p = self.position(book_id, strategy, iid, c)
        q0 = p.qty if p else ZERO
        basis = (p.cost_basis if p else ZERO) + qty * price + fee
        opened = to_iso(p.opened_at) if p and p.opened_at and q0 > 0 else ts
        self._entry(c, book_id, ts, kind, strategy, iid, qty, price, ref_type, ref_id)
        self._entry(c, book_id, ts, kind, strategy, ccy, -(qty * price), price, ref_type, ref_id)
        if fee:
            self._entry(c, book_id, ts, "fee", strategy, ccy, -fee, None, ref_type, ref_id)
        self._upsert_pos(c, book_id, strategy, iid, q0 + qty, basis, p.realized_pnl if p else ZERO,
                         (p.fees if p else ZERO) + fee, opened, ts)

    def _sell(self, c: sqlite3.Connection, book_id: str, strategy: str, iid: str, qty: Decimal, price: Decimal, fee: Decimal,
              ccy: str, ts: str, ref_type: str, ref_id: str, kind: str = "fill") -> None:
        p = self.position(book_id, strategy, iid, c)
        if p is None or p.qty < qty:
            raise LedgerError(f"보유수량 초과 매도 배분: {strategy} {iid} 보유 {p.qty if p else 0} < {qty}")
        basis_part = p.cost_basis * qty / p.qty
        realized = p.realized_pnl + qty * price - fee - basis_part
        new_qty = p.qty - qty
        new_basis = p.cost_basis - basis_part if new_qty > 0 else ZERO
        self._entry(c, book_id, ts, kind, strategy, iid, -qty, price, ref_type, ref_id)
        self._entry(c, book_id, ts, kind, strategy, ccy, qty * price, price, ref_type, ref_id)
        if fee:
            self._entry(c, book_id, ts, "fee", strategy, ccy, -fee, None, ref_type, ref_id)
        self._upsert_pos(c, book_id, strategy, iid, new_qty, new_basis, realized, p.fees + fee,
                         to_iso(p.opened_at) if new_qty > 0 else None, ts)

    def apply_fill(self, c: sqlite3.Connection, *, order: sqlite3.Row, fill_qty: Decimal, fill_price: Decimal, fee: Decimal,
                   quote_ccy: str, qty_step: Decimal, ts: datetime) -> list[LegAlloc]:
        """체결 1건을 전략별로 배분해 원장에 기록. 누적 기준 배분으로 반올림 오차가 쌓이지 않는다."""
        order_id = order["order_id"]
        book_id = order["book_id"]
        iid = order["instrument_id"]
        side = order["side"]
        legs = c.execute(
            "SELECT strategy_id, requested_qty FROM order_allocations WHERE order_id=? ORDER BY strategy_id", (order_id,)
        ).fetchall()
        if not legs:
            legs = [{"strategy_id": BOOK_STRATEGY, "requested_qty": order["qty"]}]
        req = [(r["strategy_id"], D(r["requested_qty"])) for r in legs]
        total_req = sum((q for _, q in req), ZERO)
        prev_rows = c.execute(
            "SELECT strategy_id, delta FROM ledger_entries WHERE ref_type='order' AND ref_id=? AND asset=? AND kind='fill'",
            (order_id, iid),
        ).fetchall()
        already: dict[str, Decimal] = {}
        for r in prev_rows:
            already[r["strategy_id"]] = already.get(r["strategy_id"], ZERO) + abs(D(r["delta"]))
        filled_before = sum(already.values(), ZERO)
        f_new = filled_before + fill_qty
        # 1) 누적 체결량 기준 목표 배분(요청 비율, 단위 내림)
        alloc: dict[str, Decimal] = {}
        cap: dict[str, Decimal] = {}
        for sid, q in req:
            cap[sid] = max(ZERO, q - already.get(sid, ZERO))
            target = floor_step(f_new * q / total_req, qty_step) if total_req > 0 else ZERO
            alloc[sid] = max(ZERO, min(target - already.get(sid, ZERO), cap[sid]))
        # 2) 합계가 이번 체결량과 정확히 같도록 남은 용량 기준으로 보정
        diff = fill_qty - sum(alloc.values(), ZERO)
        if diff > 0:
            for sid in sorted(cap, key=lambda s: cap[s] - alloc[s], reverse=True):
                add = min(diff, cap[sid] - alloc[sid])
                if add > 0:
                    alloc[sid] += add
                    diff -= add
            if diff > 0:  # 요청 수량을 넘는 체결(거래소 이상) → 가장 큰 요청 전략에 배정 후 검증에서 드러난다
                biggest = max(req, key=lambda x: x[1])[0]
                alloc[biggest] += diff
                diff = ZERO
        elif diff < 0:
            for sid in sorted(alloc, key=lambda s: alloc[s], reverse=True):
                sub = min(-diff, alloc[sid])
                alloc[sid] -= sub
                diff += sub
                if diff == 0:
                    break
        allocs: list[LegAlloc] = []
        fee_left = fee
        nonzero = [(sid, alloc[sid]) for sid, _ in req if alloc[sid] > 0]
        tsx = to_iso(ts)
        for i, (sid, a) in enumerate(nonzero):
            f = fee_left if i == len(nonzero) - 1 else (fee * a / fill_qty)
            fee_left -= f
            if side == "buy":
                self._buy(c, book_id, sid, iid, a, fill_price, f, quote_ccy, tsx, "order", order_id)
            else:
                self._sell(c, book_id, sid, iid, a, fill_price, f, quote_ccy, tsx, "order", order_id)
            allocs.append(LegAlloc(sid, a, f))
        total = sum((a.qty for a in allocs), ZERO)
        if total != fill_qty:
            raise LedgerError(f"체결 배분 합계 불일치 {total} != {fill_qty} ({order_id})")
        return allocs

    def internal_transfer(self, c: sqlite3.Connection, *, book_id: str, iid: str, qty: Decimal, price: Decimal,
                          from_strategy: str, to_strategy: str, quote_ccy: str, ts: datetime, ref: str) -> None:
        """전략 간 상계. 수수료 없음, 장부 전체 현금·수량 불변."""
        tsx = to_iso(ts)
        self._sell(c, book_id, from_strategy, iid, qty, price, ZERO, quote_ccy, tsx, "cross", ref, kind="internal_transfer")
        self._buy(c, book_id, to_strategy, iid, qty, price, ZERO, quote_ccy, tsx, "cross", ref, kind="internal_transfer")

    # ---------- 검증 ----------
    def verify(self, book_id: str) -> list[str]:
        problems: list[str] = []
        rows = self.db.query("SELECT strategy_id, asset, delta FROM ledger_entries WHERE book_id=?", (book_id,))
        qty: dict[tuple[str, str], Decimal] = {}
        for r in rows:
            if r["asset"] in CASH_ASSETS:
                continue
            k = (r["strategy_id"], r["asset"])
            qty[k] = qty.get(k, ZERO) + D(r["delta"])
        pos = {(p.strategy_id, p.instrument_id): p.qty for p in self.positions(book_id, include_closed=True)}
        for k in set(qty) | set(pos):
            if qty.get(k, ZERO) != pos.get(k, ZERO):
                problems.append(f"수량 불일치 {k}: 원장 {qty.get(k, ZERO)} vs 포지션 {pos.get(k, ZERO)}")
            if qty.get(k, ZERO) < 0:
                problems.append(f"음수 수량 {k}")
        for a in CASH_ASSETS:
            if self.cash(book_id, a) < Decimal("-0.000001"):
                problems.append(f"음수 현금 {a}: {self.cash(book_id, a)}")
        return problems
