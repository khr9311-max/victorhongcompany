"""시세·캔들·상품 메타데이터 저장소."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from aifund.core.money import D, dstr
from aifund.core.timeutil import Clock, parse_iso, to_iso
from aifund.db.database import Database, dumps, loads
from aifund.domain.models import Candle, Instrument, Quote


def _q(row) -> Quote:  # type: ignore[no-untyped-def]
    def dn(v: str | None) -> Decimal | None:
        return None if v is None else D(v)

    return Quote(
        instrument_id=row["instrument_id"], bid=dn(row["bid"]), ask=dn(row["ask"]), bid_size=dn(row["bid_size"]),
        ask_size=dn(row["ask_size"]), last=dn(row["last"]), turnover_24h=dn(row["turnover_24h"]),
        ts_exchange=parse_iso(row["ts_exchange"]), fetched_at=parse_iso(row["fetched_at"]),  # type: ignore[arg-type]
        source=row["source"], quote_id=int(row["id"]),
    )


class MarketStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    # --- 상품 ---
    def save_instruments(self, items: list[Instrument]) -> None:
        now = to_iso(self.clock.now())
        with self.db.tx() as c:
            for i in items:
                c.execute(
                    "INSERT INTO instruments(instrument_id, market, symbol, exchange, name, quote_ccy, base_asset, tick_policy, "
                    "fixed_tick, qty_step, min_notional, max_notional, status, meta_json, source, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id) DO UPDATE SET "
                    "exchange=excluded.exchange, name=excluded.name, quote_ccy=excluded.quote_ccy, base_asset=excluded.base_asset, "
                    "tick_policy=excluded.tick_policy, fixed_tick=excluded.fixed_tick, qty_step=excluded.qty_step, "
                    "min_notional=excluded.min_notional, max_notional=excluded.max_notional, status=excluded.status, "
                    "meta_json=excluded.meta_json, source=excluded.source, updated_at=excluded.updated_at",
                    (i.instrument_id, i.market, i.symbol, i.exchange, i.name, i.quote_ccy, i.base_asset, i.tick_policy,
                     dstr(i.fixed_tick), dstr(i.qty_step), dstr(i.min_notional), dstr(i.max_notional), i.status,
                     dumps(i.meta), i.source, now),
                )

    def instrument(self, instrument_id: str) -> Instrument | None:
        r = self.db.query_one("SELECT * FROM instruments WHERE instrument_id=?", (instrument_id,))
        if r is None:
            return None
        return Instrument(
            instrument_id=r["instrument_id"], market=r["market"], symbol=r["symbol"], exchange=r["exchange"], name=r["name"],
            quote_ccy=r["quote_ccy"], base_asset=r["base_asset"], tick_policy=r["tick_policy"],
            fixed_tick=D(r["fixed_tick"]) if r["fixed_tick"] else None, qty_step=D(r["qty_step"]),
            min_notional=D(r["min_notional"]), max_notional=D(r["max_notional"]) if r["max_notional"] else None,
            status=r["status"], source=r["source"], meta=loads(r["meta_json"], {}),
        )

    def instrument_updated_at(self, instrument_id: str) -> datetime | None:
        return parse_iso(self.db.scalar("SELECT updated_at FROM instruments WHERE instrument_id=?", (instrument_id,)))

    # --- 시세 ---
    def save_quotes(self, quotes: list[Quote]) -> list[Quote]:
        out = []
        with self.db.tx() as c:
            for q in quotes:
                cur = c.execute(
                    "INSERT INTO quotes(instrument_id, bid, ask, bid_size, ask_size, last, turnover_24h, ts_exchange, fetched_at, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (q.instrument_id, dstr(q.bid), dstr(q.ask), dstr(q.bid_size), dstr(q.ask_size), dstr(q.last),
                     dstr(q.turnover_24h), to_iso(q.ts_exchange), to_iso(q.fetched_at), q.source),
                )
                out.append(Quote(**{**q.__dict__, "quote_id": int(cur.lastrowid)}))
        return out

    def latest_quote(self, instrument_id: str) -> Quote | None:
        r = self.db.query_one("SELECT * FROM quotes WHERE instrument_id=? ORDER BY id DESC LIMIT 1", (instrument_id,))
        return None if r is None else _q(r)

    def quote_after(self, instrument_id: str, after_quote_id: int, not_before: datetime) -> Quote | None:
        """모의체결용: 주문 이후에 수집된 가장 최근 시세(미래정보 방지)."""
        r = self.db.query_one(
            "SELECT * FROM quotes WHERE instrument_id=? AND id>? AND fetched_at>? ORDER BY id DESC LIMIT 1",
            (instrument_id, after_quote_id, to_iso(not_before)),
        )
        return None if r is None else _q(r)

    def prune_quotes(self, keep_hours: int = 72) -> int:
        cutoff = to_iso(self.clock.now() - timedelta(hours=keep_hours))
        cur = self.db.execute("DELETE FROM quotes WHERE fetched_at < ?", (cutoff,))
        return cur.rowcount

    # --- 캔들 ---
    def save_candles(self, candles: list[Candle]) -> None:
        now = to_iso(self.clock.now())
        with self.db.tx() as c:
            for k in candles:
                c.execute(
                    "INSERT INTO candles(instrument_id, interval, open_time, close_time, open, high, low, close, volume, value, source, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id, interval, open_time) DO UPDATE SET "
                    "high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume, value=excluded.value, "
                    "fetched_at=excluded.fetched_at",
                    (k.instrument_id, k.interval, to_iso(k.open_time), to_iso(k.close_time), dstr(k.open), dstr(k.high),
                     dstr(k.low), dstr(k.close), dstr(k.volume), dstr(k.value), k.source, now),
                )

    def closed_candles(self, instrument_id: str, interval: str, now: datetime, limit: int = 300) -> list[Candle]:
        rows = self.db.query(
            "SELECT * FROM candles WHERE instrument_id=? AND interval=? AND close_time<=? ORDER BY open_time DESC LIMIT ?",
            (instrument_id, interval, to_iso(now), limit),
        )
        out = [
            Candle(r["instrument_id"], r["interval"], parse_iso(r["open_time"]), parse_iso(r["close_time"]),  # type: ignore[arg-type]
                   D(r["open"]), D(r["high"]), D(r["low"]), D(r["close"]), D(r["volume"]),
                   D(r["value"]) if r["value"] else None, r["source"])
            for r in rows
        ]
        out.reverse()
        return out
