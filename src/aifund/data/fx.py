"""환율 서비스. 출처·기준시각·수집시각을 기록하고, 오래된 환율은 위험 증가 주문 차단 근거가 된다.

기본 공급자: Frankfurter(ECB 기준환율, 무료·키 불필요, 영업일 1회 갱신 → 주말엔 3일 이상 지연 정상).
https://api.frankfurter.dev/v1/latest?base=USD&symbols=KRW
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from aifund.core.money import D
from aifund.core.timeutil import UTC, Clock, SystemClock, parse_iso, to_iso
from aifund.db.database import Database
from aifund.domain.models import FxRate

log = logging.getLogger(__name__)

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=KRW"
_BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class FxStatus:
    rate: FxRate | None
    age_hours: float | None
    fresh: bool
    reason: str


class FxService:
    def __init__(self, db: Database, *, provider: str = "frankfurter", clock: Clock | None = None,
                 client: httpx.AsyncClient | None = None, manual_rate: Decimal | None = None,
                 manual_as_of: str | None = None) -> None:
        self.db = db
        self.provider = provider
        self.clock = clock or SystemClock()
        self._client = client
        self.manual_rate = manual_rate
        self.manual_as_of = manual_as_of

    def record(self, rate: FxRate) -> None:
        self.db.execute(
            "INSERT INTO fx_rates(pair, rate, as_of, fetched_at, source, source_url) VALUES (?,?,?,?,?,?)",
            (rate.pair, str(rate.rate), to_iso(rate.as_of), to_iso(rate.fetched_at), rate.source, rate.source_url),
        )

    async def refresh(self) -> FxRate | None:
        now = self.clock.now()
        if self.provider == "none":
            return None
        if self.provider == "manual":
            if self.manual_rate is None:
                return None
            as_of = parse_iso(self.manual_as_of) if self.manual_as_of else now
            fx = FxRate("USDKRW", D(self.manual_rate), as_of or now, now, "manual(사용자 입력)")
            self.record(fx)
            return fx
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            r = await client.get(FRANKFURTER_URL)
            r.raise_for_status()
            d = r.json()
            day = datetime.strptime(d["date"], "%Y-%m-%d").date()
            # ECB 기준환율은 대략 중부유럽 16:00 공표 → 그 시각을 기준시각으로 둔다.
            as_of = datetime.combine(day, time(16, 0), tzinfo=_BERLIN).astimezone(UTC)
            fx = FxRate("USDKRW", D(d["rates"]["KRW"]), as_of, now, "frankfurter(ECB 기준환율)", FRANKFURTER_URL)
            self.record(fx)
            return fx
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("환율 갱신 실패: %s", exc)
            return None
        finally:
            if self._client is None:
                await client.aclose()

    def latest(self, pair: str = "USDKRW") -> FxRate | None:
        row = self.db.query_one(
            "SELECT * FROM fx_rates WHERE pair=? ORDER BY fetched_at DESC, id DESC LIMIT 1", (pair,)
        )
        if row is None:
            return None
        return FxRate(row["pair"], D(row["rate"]), parse_iso(row["as_of"]), parse_iso(row["fetched_at"]),  # type: ignore[arg-type]
                      row["source"], row["source_url"])

    def status(self, max_age_hours: int, pair: str = "USDKRW") -> FxStatus:
        fx = self.latest(pair)
        if fx is None:
            return FxStatus(None, None, False, "환율 미수집")
        age = (self.clock.now() - fx.as_of).total_seconds() / 3600
        if age > max_age_hours:
            return FxStatus(fx, age, False, f"환율 기준시각이 {age:.0f}시간 지남(한도 {max_age_hours}시간)")
        return FxStatus(fx, age, True, "정상")

    def to_krw(self, amount: Decimal, ccy: str, max_age_hours: int, haircut_pct: Decimal = Decimal(0)) -> tuple[Decimal | None, str]:
        """외화 → 원화. 환율이 없거나 오래되면 (None, 사유). haircut은 위험 증가 판단 시 보수적 가산."""
        if ccy == "KRW":
            return amount, "KRW"
        if ccy != "USD":
            return None, f"지원하지 않는 통화 {ccy}"
        st = self.status(max_age_hours)
        if not st.fresh or st.rate is None:
            return None, st.reason
        return amount * st.rate.rate * (1 + haircut_pct / 100), st.rate.source

    def usdkrw_for_costs(self, fallback: Decimal, max_age_hours: int = 24 * 7) -> tuple[Decimal, str, bool]:
        """AI 비용 환산용: (환율, 출처, 추정여부)."""
        st = self.status(max_age_hours)
        if st.fresh and st.rate is not None:
            return st.rate.rate, st.rate.source, False
        return fallback, f"설정 대체환율 {fallback}(추정)", True

    def age_text(self) -> str:
        fx = self.latest()
        if fx is None:
            return "미수집"
        hours = (self.clock.now() - fx.as_of) / timedelta(hours=1)
        return f"{fx.rate} ({fx.source}, 기준 {fx.as_of.isoformat()} / {hours:.0f}시간 전)"
