"""SQLite(WAL) 접근 계층.

- 스레드별 커넥션(웹 스레드풀·CLI 공존).
- 쓰기는 BEGIN IMMEDIATE 트랜잭션으로 직렬화한다 → 동시 주문 예약에서 현금 중복 사용 방지.
- synchronous=FULL: 전원 차단 시에도 커밋된 원장이 유실되지 않도록 한다.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

from aifund.core.timeutil import to_iso, utcnow

log = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._local = threading.local()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    # -- 커넥션 --
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """쓰기 트랜잭션. 중첩 호출 시 바깥 트랜잭션에 합류한다."""
        c = self.conn
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            try:
                yield c
            finally:
                self._local.depth -= 1
            return
        c.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")
        finally:
            self._local.depth = 0

    # -- 조회 헬퍼 --
    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.tx() as c:
            return c.execute(sql, params)

    # -- 유지보수 --
    def integrity_check(self) -> str:
        return str(self.scalar("PRAGMA integrity_check"))

    def migrate(self) -> list[str]:
        applied: list[str] = []
        c = self.conn
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        done = {r[0] for r in c.execute("SELECT version FROM schema_migrations").fetchall()}
        files = sorted(
            (p for p in resources.files("aifund.db.migrations").iterdir() if p.name.endswith(".sql")),
            key=lambda p: p.name,
        )
        for f in files:
            version = int(f.name.split("_", 1)[0])
            if version in done:
                continue
            sql = f.read_text(encoding="utf-8")
            # 스키마 변경과 버전 기록을 한 트랜잭션으로 묶는다(중간 crash 시 전부 롤백).
            record = (
                f"INSERT INTO schema_migrations(version, name, applied_at) "
                f"VALUES ({version}, '{f.name}', '{to_iso(utcnow())}');"
            )
            try:
                c.executescript("BEGIN IMMEDIATE;\n" + sql + "\n" + record + "\nCOMMIT;")
            except sqlite3.Error:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
            applied.append(f.name)
            log.info("DB 마이그레이션 적용: %s", f.name)
        return applied

    def backup_to(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()

    # -- meta --
    def get_meta(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM meta WHERE key=?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)
