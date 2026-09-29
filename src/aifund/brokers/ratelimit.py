"""중앙 호출 제한기.

봇·전략 수가 늘어도 API 한도는 늘지 않는다. 모든 거래소 호출은 (거래소, 그룹, 범위) 키 하나의
토큰 버킷을 공유한다. 429는 해당 그룹을 잠시 멈추고, 418(업비트 차단)은 길게 멈춘다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    rate: float  # 초당 허용
    capacity: float
    tokens: float
    updated: float
    blocked_until: float = 0.0


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def configure(self, key: str, per_second: float, burst: float | None = None) -> None:
        cap = burst if burst is not None else max(1.0, per_second)
        if key not in self._buckets:
            self._buckets[key] = _Bucket(per_second, cap, cap, time.monotonic())

    async def acquire(self, key: str) -> None:
        bucket = self._buckets.get(key)
        if bucket is None:
            return
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            while True:
                now = time.monotonic()
                if now < bucket.blocked_until:
                    await asyncio.sleep(bucket.blocked_until - now)
                    continue
                bucket.tokens = min(bucket.capacity, bucket.tokens + (now - bucket.updated) * bucket.rate)
                bucket.updated = now
                if bucket.tokens >= 1:
                    bucket.tokens -= 1
                    return
                await asyncio.sleep((1 - bucket.tokens) / bucket.rate)

    def update_remaining(self, key: str, remaining_this_second: int) -> None:
        """업비트 Remaining-Req 헤더의 sec 값 반영."""
        bucket = self._buckets.get(key)
        if bucket is not None and remaining_this_second <= 0:
            bucket.tokens = 0
            bucket.updated = time.monotonic()
            bucket.blocked_until = max(bucket.blocked_until, time.monotonic() + 1.0)

    def block(self, key: str, seconds: float) -> None:
        bucket = self._buckets.get(key)
        if bucket is not None:
            bucket.blocked_until = max(bucket.blocked_until, time.monotonic() + seconds)

    def blocked_for(self, key: str) -> float:
        bucket = self._buckets.get(key)
        if bucket is None:
            return 0.0
        return max(0.0, bucket.blocked_until - time.monotonic())


GLOBAL_LIMITER = RateLimiter()


def parse_upbit_remaining(header: str | None) -> tuple[str | None, int | None]:
    """'group=default; min=1800; sec=29' → ('default', 29)."""
    if not header:
        return None, None
    group = None
    sec = None
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "group":
            group = v
        elif k == "sec":
            try:
                sec = int(v)
            except ValueError:
                sec = None
    return group, sec
