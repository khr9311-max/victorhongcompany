"""데이터 스냅샷 수집. 모든 결정은 스냅샷 ID로 추적된다.

품질 검사(시세 지연, 캔들 지연·미완성, 스프레드, 유동성, 상품 상태)는 자동으로 완화하지 않는다.
휴장 중 시세가 멈춘 것은 '휴장'으로, 거래시간 중 멈춘 것은 '데이터 장애'로 구분한다.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aifund.brokers.base import MarketData
from aifund.config.settings import MarketSettings, RiskSettings
from aifund.core.ids import new_id
from aifund.core.timeutil import Clock, floor_to_interval, to_iso
from aifund.data.store import MarketStore
from aifund.db.database import Database, dumps
from aifund.domain.models import Candle, Instrument, Quote
from aifund.markets import calendar
from aifund.strategies import indicators as ind

log = logging.getLogger(__name__)

MetaEnricher = Callable[[list[Instrument]], Awaitable[list[Instrument]]]


@dataclass
class InstrumentSnap:
    instrument: Instrument
    candles: list[Candle]
    quote: Quote | None
    issues: list[str] = field(default_factory=list)  # 신규 위험 증가를 막는 문제
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def closes(self) -> list[float]:
        return [float(c.close) for c in self.candles]


@dataclass
class Snapshot:
    snapshot_id: str
    market: str
    created_at: datetime
    interval: str
    candle_close_time: datetime | None
    items: dict[str, InstrumentSnap]
    source_name: str
    is_demo: bool
    session_open: bool
    session_reason: str
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        out = {}
        for iid, it in self.items.items():
            closes = it.closes
            q = it.quote
            out[iid] = {
                "name": it.instrument.name,
                "last_close": str(it.candles[-1].close) if it.candles else None,
                "last_close_time": to_iso(it.candles[-1].close_time) if it.candles else None,
                "bars": len(it.candles),
                "ret_1bar_pct": _r(ind.pct_return(closes, 1)),
                "ret_24bar_pct": _r(ind.pct_return(closes, 24)),
                "vol_24bar_pct": _r(ind.realized_vol(closes, 24)),
                "rsi14": _r(ind.rsi(closes, 14)),
                "sma20": _r(ind.sma(closes, 20)),
                "sma60": _r(ind.sma(closes, 60)),
                "bid": str(q.bid) if q and q.bid else None,
                "ask": str(q.ask) if q and q.ask else None,
                "spread_pct": _r(float(q.spread_pct)) if q and q.spread_pct is not None else None,
                "turnover_24h": str(q.turnover_24h) if q and q.turnover_24h else None,
                "quote_fetched_at": to_iso(q.fetched_at) if q else None,
                "quote_exchange_ts": to_iso(q.ts_exchange) if q and q.ts_exchange else None,
                "status": it.instrument.status,
                "issues": it.issues,
                "notes": it.notes,
            }
        return out


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


class SnapshotCollector:
    def __init__(self, db: Database, store: MarketStore, source: MarketData, clock: Clock,
                 meta_enricher: MetaEnricher | None = None) -> None:
        self.db = db
        self.store = store
        self.source = source
        self.clock = clock
        self.meta_enricher = meta_enricher
        self.last_ok_at: dict[str, datetime] = {}

    async def refresh_instruments(self, market: str, symbols: list[str], force: bool = False) -> list[Instrument]:
        out: list[Instrument] = []
        stale: list[str] = []
        for s in symbols:
            iid = f"{market}:{s}"
            upd = self.store.instrument_updated_at(iid)
            inst = self.store.instrument(iid)
            if inst is None or force or upd is None or self.clock.now() - upd > timedelta(hours=6):
                stale.append(s)
            else:
                out.append(inst)
        if stale:
            fresh = await self.source.instruments(market, stale)
            if self.meta_enricher is not None:
                try:
                    fresh = await self.meta_enricher(fresh)
                except Exception as exc:  # 메타 보강 실패 시 공개 정보만 사용하고 기록
                    log.warning("상품 메타데이터 보강 실패: %s", exc)
            self.store.save_instruments(fresh)
            out.extend(fresh)
        order = {f"{market}:{s}": n for n, s in enumerate(symbols)}
        return sorted(out, key=lambda i: order.get(i.instrument_id, 999))

    def expected_last_close(self, market: str, interval: str, now: datetime) -> datetime | None:
        if interval == "1d":
            if market == "crypto":
                return floor_to_interval(now, 1440)
            day = calendar.last_completed_session(market, now)
            if day is None:
                return None
            from aifund.markets.calendar import _calendar  # 내부 달력 재사용

            return _calendar(market).session_close(day.isoformat()).to_pydatetime()
        return floor_to_interval(now, int(interval[:-1]))

    async def build(self, market: str, ms: MarketSettings, risk: RiskSettings, *, candle_count: int = 200) -> Snapshot:
        now = self.clock.now()
        errors: list[str] = []
        session = calendar.session_info(market, now)
        instruments = await self.refresh_instruments(market, ms.instruments)
        quotes: dict[str, Quote] = {}
        try:
            for q in self.store.save_quotes(await self.source.quotes(instruments)):
                quotes[q.instrument_id] = q
        except Exception as exc:
            errors.append(f"시세 조회 실패: {exc}")
        expected = self.expected_last_close(market, ms.candle, now)
        items: dict[str, InstrumentSnap] = {}
        for inst in instruments:
            issues: list[str] = []
            notes: list[str] = []
            try:
                fetched = await self.source.candles(inst, ms.candle, candle_count)
                self.store.save_candles(fetched)
            except Exception as exc:
                issues.append(f"캔들 조회 실패: {exc}")
            closed = self.store.closed_candles(inst.instrument_id, ms.candle, now, 400)
            if not closed:
                issues.append("완성된 캔들 없음")
            elif expected is not None and closed[-1].close_time < expected - timedelta(seconds=1):
                lag = (expected - closed[-1].close_time).total_seconds()
                issues.append(f"최근 완성봉 누락/지연({lag / 60:.0f}분)")
            q = quotes.get(inst.instrument_id)
            if q is None or q.ask is None or q.bid is None:
                issues.append("호가 없음")
            else:
                if q.ts_exchange is not None:
                    age = (now - q.ts_exchange).total_seconds()
                    if age > risk.max_quote_age_sec * 4:
                        if session.is_open:
                            issues.append(f"거래시간 중 호가 갱신 지연({age:.0f}초) → 데이터 장애")
                        else:
                            notes.append(f"휴장 중 시세 정지({session.reason})")
                if q.spread_pct is not None and q.spread_pct > risk.max_spread_pct:
                    issues.append(f"스프레드 과대 {q.spread_pct:.3f}% > {risk.max_spread_pct}%")
                if market == "crypto":
                    if q.turnover_24h is None or q.turnover_24h < risk.min_24h_turnover_krw:
                        issues.append("24시간 거래대금 부족")
                else:
                    notes.append("거래대금 기준은 코인에만 적용")
            if inst.status not in ("active",):
                issues.append(f"상품 상태: {inst.status}")
            if not session.is_open and market != "crypto":
                notes.append(f"시장 {session.reason}")
            items[inst.instrument_id] = InstrumentSnap(inst, closed, q, issues, notes)
        close_time = max((it.candles[-1].close_time for it in items.values() if it.candles), default=None)
        snap = Snapshot(new_id("snap"), market, now, ms.candle, close_time, items, self.source.source_name,
                        getattr(self.source, "is_demo", False), session.is_open, session.reason, errors)
        data = snap.summary()
        status = {"errors": errors, "session": session.reason, "source": self.source.source_name, "demo": snap.is_demo}
        content_hash = hashlib.sha256(dumps(data).encode()).hexdigest()[:16]
        self.db.execute(
            "INSERT INTO snapshots(snapshot_id, market, created_at, candle_close_time, data_json, status_json, content_hash) "
            "VALUES (?,?,?,?,?,?,?)",
            (snap.snapshot_id, market, to_iso(now), to_iso(close_time), dumps(data), dumps(status), content_hash),
        )
        if not errors and any(it.ok for it in items.values()):
            self.last_ok_at[market] = now
        return snap
