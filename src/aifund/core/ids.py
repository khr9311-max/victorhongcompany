"""정렬 가능한 고유 ID 생성."""

from __future__ import annotations

import time
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}-{time.time_ns():x}-{uuid.uuid4().hex[:10]}"


def client_order_id() -> str:
    """거래소에 보내는 멱등 키. 업비트 identifier로 사용된다(재사용 불가)."""
    return "af" + uuid.uuid4().hex
