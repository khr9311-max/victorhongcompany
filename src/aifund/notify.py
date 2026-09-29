"""알림. 기본은 로컬 로그·DB 기록뿐이다.

외부 알림(텔레그램 봇 / 웹훅)은 사용자가 .env에 '본인' 수신처를 설정하고 설정에서 켠 경우에만 보낸다.
임의 수신처로는 보내지 않는다. 맥북 전체가 꺼지면 맥북 스스로는 장애 알림을 보낼 수 없다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx

from aifund.config.settings import Settings
from aifund.core.secrets import ModeSecrets
from aifund.core.timeutil import Clock, to_iso
from aifund.db.database import Database

log = logging.getLogger(__name__)
_SEV = {"info": 0, "warning": 1, "critical": 2}


class Notifier:
    def __init__(self, db: Database, clock: Clock, settings_fn: Callable[[], Settings], secrets: ModeSecrets, mode: str) -> None:
        self.db = db
        self.clock = clock
        self.settings_fn = settings_fn
        self.secrets = secrets
        self.mode = mode
        self.queue: asyncio.Queue[tuple[str, str, str]] | None = None
        self._sent: list[float] = []

    def channels(self) -> list[str]:
        s = self.settings_fn().notify
        ch = ["log"]
        if s.telegram and self.secrets.telegram_bot_token and self.secrets.telegram_chat_id:
            ch.append("telegram")
        if s.webhook and self.secrets.webhook_url:
            ch.append("webhook")
        return ch

    def notify(self, severity: str, title: str, body: str) -> None:
        chans = self.channels()
        external = [c for c in chans if c != "log"] if _SEV.get(severity, 1) >= _SEV[self.settings_fn().notify.min_severity] else []
        self.db.execute("INSERT INTO notifications(ts, severity, title, body, channels, status) VALUES (?,?,?,?,?,?)",
                        (to_iso(self.clock.now()), severity, title[:200], body[:2000], ",".join(["log", *external]),
                         "queued" if external else "logged"))
        log.log(logging.ERROR if severity == "critical" else logging.WARNING if severity == "warning" else logging.INFO,
                "[알림] %s - %s", title, body[:300])
        if external and self.queue is not None:
            try:
                self.queue.put_nowait((severity, title, body))
            except asyncio.QueueFull:
                log.warning("알림 대기열 가득 참: 외부 전송 생략")

    async def run(self) -> None:
        self.queue = asyncio.Queue(maxsize=200)
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                severity, title, body = await self.queue.get()
                now = time.monotonic()
                self._sent = [t for t in self._sent if now - t < 3600]
                if len(self._sent) >= 30:
                    log.warning("외부 알림 시간당 한도 초과: 로컬 기록만 유지")
                    continue
                text = f"[aifund:{self.mode}] {title}\n{body}"[:3500]
                for ch in self.channels():
                    try:
                        if ch == "telegram":
                            url = f"https://api.telegram.org/bot{self.secrets.telegram_bot_token}/sendMessage"
                            await client.post(url, json={"chat_id": self.secrets.telegram_chat_id, "text": text})
                        elif ch == "webhook" and self.secrets.webhook_url:
                            await client.post(self.secrets.webhook_url, json={"severity": severity, "title": title, "body": body,
                                                                               "mode": self.mode})
                    except httpx.HTTPError as exc:
                        log.warning("외부 알림 전송 실패(%s): %s", ch, exc.__class__.__name__)
                self._sent.append(now)
