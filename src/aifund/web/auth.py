"""대시보드 접근 통제.

- 관리자 토큰(.env의 AIFUND_ADMIN_TOKEN)으로 로그인 → HMAC 서명 세션 쿠키(HttpOnly, SameSite=Strict).
- 상태 변경 요청은 로그인 + CSRF 토큰 + Host/Origin 검사를 모두 통과해야 한다.
- 기본 바인딩은 127.0.0.1. 원격 바인딩 시 AIFUND_ALLOW_REMOTE=1과 32자 이상 토큰이 필요하고, 조회에도 로그인이 필요하다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request

COOKIE = "aifund_session"
TTL = 12 * 3600


def _key(admin_token: str, mode: str) -> bytes:
    return hashlib.sha256(f"aifund-session|{mode}|{admin_token}".encode()).digest()


def make_session(admin_token: str, mode: str) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + TTL, "csrf": csrf}).encode()).decode()
    sig = hmac.new(_key(admin_token, mode), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}", csrf


def read_session(cookie: str | None, admin_token: str | None, mode: str) -> dict[str, Any] | None:
    if not cookie or not admin_token or "." not in cookie:
        return None
    payload, sig = cookie.rsplit(".", 1)
    good = hmac.new(_key(admin_token, mode), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


def check_host(request: Request, allowed: list[str], port: int) -> None:
    host = (request.headers.get("host") or "").lower()
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    if name not in [a.lower() for a in allowed]:
        raise HTTPException(status_code=400, detail="허용되지 않은 Host 헤더(DNS 재바인딩 방지)")
    origin = request.headers.get("origin")
    if request.method == "POST" and origin:
        o = origin.split("://", 1)[-1].lower()
        o_name = o.rsplit(":", 1)[0] if not o.startswith("[") else o.split("]")[0] + "]"
        if o_name not in [a.lower() for a in allowed]:
            raise HTTPException(status_code=403, detail="허용되지 않은 Origin")


def token_ok(given: str, expected: str | None) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode(), (expected or "").encode())
