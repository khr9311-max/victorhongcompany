"""영속 제어 플래그와 사고(incident) 기록.

플래그는 DB에 저장되어 앱 재시작으로 해제되지 않는다. 해제는 사람(CLI/웹)의 명시적 동작으로만 한다.
(대사 차단·인증 오류 플래그는 문제가 해소되면 시스템이 스스로 해제할 수 있다.)
"""

from __future__ import annotations

import logging
from typing import Any

from aifund.core.timeutil import Clock, to_iso
from aifund.db.database import Database, dumps

log = logging.getLogger(__name__)

HALT_GLOBAL = "halt:global"
SYSTEM_ACTORS = {"system", "reconciler", "risk"}


def halt_market_key(market: str) -> str:
    return f"halt:market:{market}"


def recon_block_key(account_id: str) -> str:
    return f"recon_block:{account_id}"


def auth_error_key(account_id: str) -> str:
    return f"auth_error:{account_id}"


class Flags:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def set(self, key: str, reason: str, actor: str, value: str = "1") -> None:
        now = to_iso(self.clock.now())
        with self.db.tx() as c:
            prev = c.execute("SELECT value FROM control_flags WHERE key=?", (key,)).fetchone()
            c.execute(
                "INSERT INTO control_flags(key, value, reason, actor, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, reason=excluded.reason, actor=excluded.actor, updated_at=excluded.updated_at",
                (key, value, reason, actor, now),
            )
            if prev is None:
                c.execute("INSERT INTO control_events(ts, actor, action, scope, detail_json) VALUES (?,?,?,?,?)",
                          (now, actor, "flag_set", key, dumps({"reason": reason})))
        if prev is None:
            log.warning("제어 플래그 설정 %s (%s): %s", key, actor, reason)

    def clear(self, key: str, actor: str, reason: str = "") -> bool:
        now = to_iso(self.clock.now())
        with self.db.tx() as c:
            cur = c.execute("DELETE FROM control_flags WHERE key=?", (key,))
            if cur.rowcount:
                c.execute("INSERT INTO control_events(ts, actor, action, scope, detail_json) VALUES (?,?,?,?,?)",
                          (now, actor, "flag_cleared", key, dumps({"reason": reason})))
                log.warning("제어 플래그 해제 %s (%s)", key, actor)
                return True
        return False

    def get(self, key: str) -> dict[str, Any] | None:
        r = self.db.query_one("SELECT * FROM control_flags WHERE key=?", (key,))
        return None if r is None else dict(r)

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM control_flags ORDER BY key")]

    def halted(self, market: str) -> str | None:
        for key in (HALT_GLOBAL, halt_market_key(market)):
            f = self.get(key)
            if f:
                return f"신규 매수 중지({'전체' if key == HALT_GLOBAL else market}): {f['reason']}"
        return None

    def account_blocks(self, account_id: str) -> list[str]:
        out = []
        for key in (recon_block_key(account_id), auth_error_key(account_id)):
            f = self.get(key)
            if f:
                out.append(f"{key}: {f['reason']}")
        return out

    def event(self, actor: str, action: str, scope: str, detail: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO control_events(ts, actor, action, scope, detail_json) VALUES (?,?,?,?,?)",
                        (to_iso(self.clock.now()), actor, action, scope, dumps(detail)))


class Incidents:
    def __init__(self, db: Database, clock: Clock, notifier: Any = None) -> None:
        self.db = db
        self.clock = clock
        self.notifier = notifier

    def open(self, category: str, detail: str, *, severity: str = "warning", market: str | None = None,
             account_id: str | None = None, book_id: str | None = None) -> None:
        """같은 미해결 사고는 중복 기록·알림하지 않는다."""
        now = self.clock.now()
        recent = self.db.query_one(
            "SELECT id, ts FROM incidents WHERE category=? AND COALESCE(account_id,'')=? AND COALESCE(market,'')=? "
            "AND detail=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
            (category, account_id or "", market or "", detail),
        )
        if recent is not None:
            return
        self.db.execute(
            "INSERT INTO incidents(ts, severity, category, market, account_id, book_id, detail) VALUES (?,?,?,?,?,?,?)",
            (to_iso(now), severity, category, market, account_id, book_id, detail),
        )
        log.log(logging.ERROR if severity == "critical" else logging.WARNING, "[사고:%s] %s", category, detail)
        if self.notifier is not None:
            self.notifier.notify(severity, f"[{category}] {market or account_id or ''}".strip(), detail)

    def resolve(self, category: str, account_id: str | None = None, market: str | None = None) -> None:
        self.db.execute(
            "UPDATE incidents SET resolved_at=? WHERE category=? AND resolved_at IS NULL AND COALESCE(account_id,'')=? AND COALESCE(market,'')=?",
            (to_iso(self.clock.now()), category, account_id or "", market or ""),
        )

    def count(self, book_id: str | None = None, since: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM incidents WHERE 1=1"
        params: list[Any] = []
        if since:
            sql += " AND ts>=?"
            params.append(since)
        return int(self.db.scalar(sql, tuple(params)))
