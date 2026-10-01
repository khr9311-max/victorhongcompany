"""비밀값 로딩과 로그 마스킹.

- 비밀은 .env / 환경변수에서만 읽는다. DB·설정파일·AI 입력에는 넣지 않는다.
- 모드별로 필요한 키만 노출한다(live 키는 live 모드에서만, 모의 키는 broker_sandbox에서만).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_REGISTERED: set[str] = set()


def load_dotenv(path: Path) -> None:
    """의존성 없는 .env 로더. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return None
    v = v.strip()
    register_secret(v)
    return v


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _REGISTERED.add(value)


@dataclass(frozen=True)
class UpbitCreds:
    access_key: str
    secret_key: str


@dataclass(frozen=True)
class KisCreds:
    app_key: str
    app_secret: str
    account_no: str  # 계좌번호 앞 8자리
    account_product: str  # 뒤 2자리 (주식 위탁 01)
    env: str  # "real" | "demo"


@dataclass(frozen=True, repr=False)
class KiwoomCreds:
    app_key: str
    app_secret: str
    env: str

    def __post_init__(self):
        if self.env not in ("mock", "real"):
            raise ValueError("키움 환경은 mock 또는 real이어야 합니다")


@dataclass(frozen=True)
class ModeSecrets:
    mode: str
    upbit: UpbitCreds | None = None
    kis_trade: KisCreds | None = None
    kis_data: KisCreds | None = None
    kiwoom_data: KiwoomCreds | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    naver_client_id: str | None = None
    naver_client_secret: str | None = None
    admin_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    webhook_url: str | None = None
    dart_api_key: str | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, str]:
        """화면·로그용: 값은 절대 노출하지 않고 설정 여부만."""

        def s(x: object) -> str:
            return "설정됨" if x else "미설정"

        return {
            "업비트 주문 키": s(self.upbit),
            "KIS 주문 키": s(self.kis_trade),
            "KIS 시세 키": s(self.kis_data),
            "키움 조회 키": s(self.kiwoom_data),
            "Anthropic API 키": s(self.anthropic_api_key),
            "Gemini API 키": s(self.gemini_api_key),
            "네이버 뉴스 키": s(self.naver_client_id and self.naver_client_secret),
            "관리자 토큰": s(self.admin_token),
            "텔레그램 알림": s(self.telegram_bot_token and self.telegram_chat_id),
            "웹훅 알림": s(self.webhook_url),
            "DART 키": s(self.dart_api_key),
        }


def _kis(prefix: str, env: str) -> KisCreds | None:
    key, sec = _env(f"{prefix}_APP_KEY"), _env(f"{prefix}_APP_SECRET")
    acct = _env(f"{prefix}_ACCOUNT_NO")
    prod = os.environ.get(f"{prefix}_ACCOUNT_PRODUCT", "01").strip() or "01"
    if not (key and sec):
        return None
    return KisCreds(app_key=key, app_secret=sec, account_no=acct or "", account_product=prod, env=env)


def load_mode_secrets(mode: str) -> ModeSecrets:
    notes: list[str] = []
    upbit = None
    kis_trade = None
    kis_data = None
    if mode == "live":
        a, s = _env("UPBIT_LIVE_ACCESS_KEY"), _env("UPBIT_LIVE_SECRET_KEY")
        if a and s:
            upbit = UpbitCreds(a, s)
        kis_trade = _kis("KIS_LIVE", "real")
    elif mode == "broker_sandbox":
        kis_trade = _kis("KIS_SANDBOX", "demo")
        notes.append("업비트는 공식 모의투자 환경이 확인되지 않아 broker_sandbox에서 코인 주문을 지원하지 않습니다.")
    if mode != "offline_demo":
        # 시세 조회 전용 키(주문 권한과 무관). 없으면 live/sandbox 키를 시세에만 재사용.
        kis_data = _kis("KIS_DATA", "real") or (kis_trade if kis_trade and kis_trade.env == "real" else None)
    kiwoom_data = None
    if mode != "offline_demo":
        key, secret = _env("KIWOOM_DATA_APP_KEY"), _env("KIWOOM_DATA_APP_SECRET")
        env = (os.environ.get("KIWOOM_DATA_ENV") or "mock").strip().lower()
        if key and secret:
            # 환경 값은 키움 키가 있을 때만 검사한다(비워 두면 mock).
            if env not in ("mock", "real"):
                raise ValueError("KIWOOM_DATA_ENV는 mock 또는 real이어야 합니다")
            kiwoom_data = KiwoomCreds(key, secret, env)
    anthropic_key = _env("ANTHROPIC_API_KEY") if mode != "offline_demo" else None
    return ModeSecrets(
        mode=mode,
        upbit=upbit,
        kis_trade=kis_trade,
        kis_data=kis_data,
        kiwoom_data=kiwoom_data,
        anthropic_api_key=anthropic_key,
        gemini_api_key=_env("GEMINI_API_KEY") if mode != "offline_demo" else None,
        naver_client_id=_env("NAVER_CLIENT_ID") if mode != "offline_demo" else None,
        naver_client_secret=_env("NAVER_CLIENT_SECRET") if mode != "offline_demo" else None,
        admin_token=_env("AIFUND_ADMIN_TOKEN"),
        telegram_bot_token=_env("AIFUND_TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.environ.get("AIFUND_TELEGRAM_CHAT_ID") or None,
        webhook_url=_env("AIFUND_WEBHOOK_URL"),
        dart_api_key=_env("DART_API_KEY"),
        notes=notes,
    )


_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-_.=]+"),
    re.compile(r"(\"?(?:appsecret|appkey|secret_key|access_key|authorization|api[_-]?key)\"?\s*[:=]\s*\"?)[^\s\",}]+", re.I),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
]


def redact(text: str) -> str:
    for secret in _REGISTERED:
        if secret in text:
            text = text.replace(secret, "***")
    for pat in _PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        red = redact(msg)
        if red != msg:
            record.msg = red
            record.args = None
        return True
