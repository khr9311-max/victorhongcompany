"""테스트 공통 도구. 실제 거래소에는 어떤 요청도 보내지 않는다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from aifund.brokers.base import BrokerAdapter
from aifund.brokers.fake import FakeBroker
from aifund.config.settings import Settings
from aifund.control.flags import Flags, Incidents
from aifund.core.ids import new_id
from aifund.core.money import D
from aifund.core.timeutil import UTC, ManualClock, to_iso
from aifund.data.fx import FxService
from aifund.data.store import MarketStore
from aifund.db.database import Database
from aifund.domain.models import Candle, Instrument, Quote, Side, TradeFill
from aifund.execution.executor import OrderExecutor, OrderIntent
from aifund.ledger.ledger import Ledger
from aifund.markets.rules import UPBIT_VOLUME_STEP

T0 = datetime(2026, 3, 2, 1, 0, tzinfo=UTC)


def inst(symbol: str = "KRW-TEST", market: str = "crypto", ccy: str = "KRW", tick_policy: str = "upbit_krw",
         step: Decimal = UPBIT_VOLUME_STEP, min_notional: Decimal = D(5000)) -> Instrument:
    return Instrument(f"{market}:{symbol}", market, symbol, "T", symbol, ccy, symbol.split("-")[-1], tick_policy, None, step,
                      min_notional, None, "active", "test")


@dataclass
class Env:
    db: Database
    clock: ManualClock
    settings: Settings
    store: MarketStore
    fx: FxService
    ledger: Ledger
    flags: Flags
    incidents: Incidents
    broker: BrokerAdapter
    executor: OrderExecutor
    inst: Instrument

    def quote(self, bid: str = "9990", ask: str = "10000", size: str = "100", iid: str | None = None) -> Quote:
        q = Quote(iid or self.inst.instrument_id, D(bid), D(ask), D(size), D(size), (D(bid) + D(ask)) / 2, D(10**10),
                  self.clock.now(), self.clock.now(), "test")
        return self.store.save_quotes([q])[0]

    def intent(self, qty: str = "5", side: Side = Side.BUY, price: str = "10000",
               legs: list[tuple[str, Decimal]] | None = None, book: str = "b") -> OrderIntent:
        iid = new_id("int")
        self.db.execute(
            "INSERT INTO intents(intent_id, book_id, instrument_id, side, qty, ref_price, notional_krw, risk_increasing, purpose, "
            "status, allocations_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, book, self.inst.instrument_id, side.value, qty, price, "0", int(side == Side.BUY), "rebalance", "approved", "[]",
             to_iso(self.clock.now())))
        return OrderIntent(iid, book, self.broker.account_id, self.inst.market, self.inst, side, D(qty), D(price), D(price),
                           legs or [("trend_sma", D(qty))], side == Side.BUY)


def make_env(tmp: Path, broker: BrokerAdapter | None = None, settings: Settings | None = None, principal: str = "300000",
             db_path: Path | None = None) -> Env:
    db = Database(db_path or (tmp / f"{new_id('t')}.sqlite3"))
    db.migrate()
    clock = ManualClock(T0)
    s = settings or Settings()
    store = MarketStore(db, clock)
    fx = FxService(db, clock=clock, provider="none")
    ledger = Ledger(db, clock)
    ledger.create_book("b", kind="operating", setting="A", principal_krw=D(principal), virtual=True, account_id="acct",
                       description="test")
    i = inst()
    store.save_instruments([i])
    b = broker or FakeBroker("acct")
    flags = Flags(db, clock)
    inc = Incidents(db, clock)
    ex = OrderExecutor(db=db, ledger=ledger, broker=b, store=store, fx=fx, flags=flags, incidents=inc, clock=clock,
                       settings_fn=lambda: s, mode="internal_paper")
    env = Env(db, clock, s, store, fx, ledger, flags, inc, b, ex, i)
    env.quote()
    return env


def trade(i: str, q: str, p: str = "10000", fee: str = "0") -> TradeFill:
    return TradeFill(i, D(q), D(p), D(fee), None)


def candles_series(iid: str, closes: list[float], start: datetime = T0, minutes: int = 60) -> list[Candle]:
    out = []
    prev = closes[0]
    for k, c in enumerate(closes):
        o_t = start + timedelta(minutes=minutes * k)
        hi, lo = max(prev, c) * 1.001, min(prev, c) * 0.999
        out.append(Candle(iid, f"{minutes}m", o_t, o_t + timedelta(minutes=minutes), D(round(prev)), D(round(hi)), D(round(lo)),
                          D(round(c)), D(10), D(round(c * 10)), "REPLAY"))
        prev = c
    return out
