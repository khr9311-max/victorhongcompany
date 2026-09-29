"""계좌 대사.

1) 미종결 주문을 거래소 기준으로 갱신(상태 불명 해소 포함)
2) 거래소 미체결 목록 ↔ DB: 봇이 모르는 미체결 주문 = 다른 프로그램/수동 주문 → 소유분 구분 불가 → 차단
3) 보유 수량: 거래소 보유 = (LIVE 활성화 시점 기존 보유분) + (봇 장부 수량) 이어야 함
4) 현금: 거래소 현금 ≥ 봇 장부 현금(입금은 한도를 늘리지 않으며, 부족은 외부 사용·출금 가능성)
불일치가 있으면 recon_block 플래그로 해당 계좌의 자동 주문을 막고 해결 방법을 표시한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from aifund.brokers.base import BrokerAdapter, BrokerError
from aifund.control.flags import Flags, Incidents, recon_block_key
from aifund.core.money import D, ZERO
from aifund.core.timeutil import Clock, to_iso
from aifund.db.database import Database, dumps
from aifund.domain.models import Instrument
from aifund.execution.executor import OrderExecutor
from aifund.ledger.ledger import Ledger

log = logging.getLogger(__name__)

RESOLUTION_HINT = (
    "해결: ① 봇 전용 서브포켓/계좌를 사용하거나 ② 다른 프로그램·수동 주문을 멈춘 뒤 "
    "`aifund reconcile` 재실행, ③ 기존 보유분이면 `aifund live baseline --account <계좌>`로 명시적으로 기준을 다시 잡으세요."
)


@dataclass
class ReconResult:
    account_id: str
    ok: bool
    mismatches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked_at: datetime | None = None


def balance_key(broker: BrokerAdapter, inst: Instrument) -> str:
    return inst.base_asset if broker.name == "upbit" else inst.instrument_id


class Reconciler:
    def __init__(self, *, db: Database, ledger: Ledger, executor: OrderExecutor, broker: BrokerAdapter, flags: Flags,
                 incidents: Incidents, clock: Clock, book_ids: list[str], instruments: list[Instrument],
                 cash_check: bool = True) -> None:
        self.db = db
        self.ledger = ledger
        self.executor = executor
        self.broker = broker
        self.flags = flags
        self.incidents = incidents
        self.clock = clock
        self.book_ids = book_ids
        self.instruments = instruments
        self.cash_check = cash_check

    def baseline(self, asset: str) -> Decimal:
        r = self.db.query_one("SELECT qty FROM account_baselines WHERE account_id=? AND asset=?", (self.broker.account_id, asset))
        return D(r["qty"]) if r else ZERO

    async def capture_baseline(self, actor: str, note: str) -> dict[str, str]:
        """현재 거래소 보유분 중 봇 장부에 없는 수량을 '기존 보유분'으로 기록(봇이 매도하지 않음)."""
        bals = await self.broker.balances()
        out = {}
        now = to_iso(self.clock.now())
        with self.db.tx() as c:
            for inst in self.instruments:
                key = balance_key(self.broker, inst)
                total = bals[key].total if key in bals else ZERO
                bot = sum((self.ledger.book_qty(b, inst.instrument_id, c) for b in self.book_ids), ZERO)
                ext = max(ZERO, total - bot)
                c.execute("INSERT INTO account_baselines(account_id, asset, qty, captured_at, note) VALUES (?,?,?,?,?) "
                          "ON CONFLICT(account_id, asset) DO UPDATE SET qty=excluded.qty, captured_at=excluded.captured_at, note=excluded.note",
                          (self.broker.account_id, key, str(ext), now, note))
                out[key] = str(ext)
            c.execute("INSERT INTO control_events(ts, actor, action, scope, detail_json) VALUES (?,?,?,?,?)",
                      (now, actor, "baseline_captured", self.broker.account_id, dumps(out)))
        return out

    async def run(self, reason: str, *, record_flags: bool = True) -> ReconResult:
        res = ReconResult(self.broker.account_id, True, checked_at=self.clock.now())
        acct = self.broker.account_id
        try:
            await self.executor.poll()
            unknown = self.executor.unknown_count()
            if unknown:
                res.mismatches.append(f"상태 불명 주문 {unknown}건(거래소 조회로 해소될 때까지 위험 증가 금지)")
            db_open = {r["broker_order_id"] for r in self.executor.open_orders() if r["broker_order_id"]}
            if self.broker.capabilities.open_orders:
                broker_open = await self.broker.open_orders(self.instruments)
                foreign = [o for o in broker_open if o.broker_order_id not in db_open
                           and not (o.client_order_id or "").startswith("af")]
                if foreign:
                    res.mismatches.append(f"봇이 모르는 미체결 주문 {len(foreign)}건: 다른 프로그램/수동 주문 가능성")
            else:
                res.notes.append("이 거래소(환경)는 미체결 목록 조회를 지원하지 않아 외부 주문 감지 제한")
            bals = await self.broker.balances()
            tol_mult = Decimal(10)
            for inst in self.instruments:
                key = balance_key(self.broker, inst)
                total = bals[key].total if key in bals else ZERO
                bot = sum((self.ledger.book_qty(b, inst.instrument_id) for b in self.book_ids), ZERO)
                expected = self.baseline(key) + bot
                if abs(total - expected) > inst.qty_step * tol_mult:
                    res.mismatches.append(
                        f"{inst.instrument_id} 수량 불일치: 거래소 {total} ≠ 기존보유 {self.baseline(key)} + 봇 {bot}")
            ccys = {i.quote_ccy for i in self.instruments} if self.cash_check else set()
            if not self.cash_check:
                res.notes.append("같은 통화를 여러 계좌가 나눠 쓰므로 현금 대사는 생략(주문 전 계좌 주문가능금액으로 확인)")
            for ccy in ccys:
                bot_cash = sum((self.ledger.cash(b, ccy) for b in self.book_ids), ZERO)
                if bot_cash <= 0:
                    continue
                broker_cash = bals[ccy].total if ccy in bals else None
                if broker_cash is None:
                    res.notes.append(f"{ccy} 현금 잔고를 거래소에서 확인하지 못함")
                    continue
                if broker_cash + Decimal("1") < bot_cash:
                    res.mismatches.append(f"{ccy} 현금 부족: 거래소 {broker_cash} < 봇 장부 {bot_cash} (외부 사용·출금 가능성)")
        except BrokerError as exc:
            res.mismatches.append(f"거래소 조회 실패: {exc}")
        res.ok = not res.mismatches
        self.db.execute("INSERT INTO reconciliations(account_id, ts, ok, mismatches_json, detail_json) VALUES (?,?,?,?,?)",
                        (acct, to_iso(self.clock.now()), int(res.ok), dumps(res.mismatches), dumps({"reason": reason, "notes": res.notes})))
        key = recon_block_key(acct)
        if not record_flags:
            return res
        if res.ok:
            if self.flags.get(key):
                self.flags.clear(key, "reconciler", "대사 일치 확인")
            self.incidents.resolve("reconcile_mismatch", account_id=acct)
        else:
            only_unknown = all(m.startswith("상태 불명") for m in res.mismatches)
            if not only_unknown:
                self.flags.set(key, "; ".join(res.mismatches)[:500] + " | " + RESOLUTION_HINT, "reconciler")
                self.incidents.open("reconcile_mismatch", "; ".join(res.mismatches)[:800], severity="critical", account_id=acct)
        return res
