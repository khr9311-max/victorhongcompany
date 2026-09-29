"""시장 운영시간·휴장일.

- 코인: 24시간(단, 거래소 점검은 주문 오류 market_offline 등으로 감지).
- 한국: exchange_calendars XKRX (Asia/Seoul).
- 미국: exchange_calendars XNYS (America/New_York, 서머타임·조기폐장 반영).
달력 로딩에 실패하면 '알 수 없음 → 닫힘'으로 처리(위험 증가 주문 차단)한다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from aifund.core.timeutil import UTC

log = logging.getLogger(__name__)

_CAL_CODES = {"kr_stock": "XKRX", "us_stock": "XNYS"}
_cache: dict[str, Any] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class SessionInfo:
    market: str
    is_open: bool
    reason: str
    session_open: datetime | None = None
    session_close: datetime | None = None
    next_open: datetime | None = None
    calendar_ok: bool = True


def _calendar(market: str) -> Any:
    code = _CAL_CODES[market]
    with _lock:
        if code not in _cache:
            import exchange_calendars as xcals  # 무거운 의존성은 지연 로딩

            _cache[code] = xcals.get_calendar(code)
        return _cache[code]


def _ts(dt: datetime) -> Any:
    import pandas as pd

    return pd.Timestamp(dt.astimezone(UTC))


def session_info(market: str, now: datetime) -> SessionInfo:
    if market == "crypto":
        return SessionInfo(market, True, "24시간 거래")
    try:
        cal = _calendar(market)
        ts = _ts(now).floor("min")
        if cal.is_open_on_minute(ts):
            session = cal.minute_to_session(ts)
            o = cal.session_open(session).to_pydatetime()
            c = cal.session_close(session).to_pydatetime()
            return SessionInfo(market, True, "정규장 운영 중", o, c)
        nxt = cal.next_open(ts).to_pydatetime()
        today_local = now.astimezone(cal.tz).date()
        if cal.is_session(today_local.isoformat()):
            o = cal.session_open(today_local.isoformat()).to_pydatetime()
            c = cal.session_close(today_local.isoformat()).to_pydatetime()
            reason = "장 시작 전" if now < o else "장 마감"
            return SessionInfo(market, False, reason, o, c, nxt)
        wd = today_local.weekday()
        reason = "휴장(주말)" if wd >= 5 else "휴장(공휴일)"
        return SessionInfo(market, False, reason, None, None, nxt)
    except Exception as exc:  # 달력 범위 밖·로딩 실패
        log.warning("거래 달력 확인 실패(%s): %s", market, exc)
        return SessionInfo(market, False, f"달력 확인 실패: {exc}", calendar_ok=False)


def last_completed_session(market: str, now: datetime) -> date | None:
    """now 이전에 종가가 확정된 마지막 거래일(현지 날짜)."""
    if market == "crypto":
        return None
    cal = _calendar(market)
    ts = _ts(now)
    day = now.astimezone(cal.tz).date()
    for _ in range(15):
        iso = day.isoformat()
        if cal.is_session(iso) and cal.session_close(iso) <= ts:
            return day
        day -= timedelta(days=1)
    return None


def current_session_open(market: str, now: datetime) -> datetime | None:
    info = session_info(market, now)
    return info.session_open if info.is_open else None
