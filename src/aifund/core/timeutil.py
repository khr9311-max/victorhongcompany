"""시간 유틸리티. 내부 저장·계산은 항상 UTC, 화면 표시는 Asia/Seoul."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """테스트·재생용 수동 시계."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("시각에는 시간대가 필요합니다")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(UTC)

    def advance(self, seconds: float = 0, **kwargs: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds, **kwargs)
        return self._now


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime은 저장할 수 없습니다")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def kst(dt: datetime | None) -> datetime | None:
    return None if dt is None else dt.astimezone(KST)


def kst_str(dt: datetime | str | None, with_seconds: bool = True) -> str:
    if isinstance(dt, str):
        dt = parse_iso(dt)
    if dt is None:
        return "-"
    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return dt.astimezone(KST).strftime(fmt) + " KST"


def kst_day(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d")


def kst_month(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m")


def kst_midnight_utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=KST).astimezone(UTC)


def age_seconds(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    return (now - then).total_seconds()


def floor_to_interval(dt: datetime, minutes: int) -> datetime:
    """UTC 기준으로 분 단위 봉 경계에 내림."""
    dt = dt.astimezone(UTC)
    epoch = int(dt.timestamp())
    step = minutes * 60
    return datetime.fromtimestamp(epoch - (epoch % step), UTC)
