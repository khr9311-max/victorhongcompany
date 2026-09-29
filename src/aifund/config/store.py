"""설정 버전 저장소. 모든 변경은 버전·작성자·사유와 함께 기록된다."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from aifund.config.settings import Settings, file_hash, load_settings_file
from aifund.core.timeutil import to_iso, utcnow
from aifund.db.database import Database, dumps

log = logging.getLogger(__name__)

ALLOWED_ACTORS = {"setup", "config_file", "cli", "web", "candidate_promotion", "candidate_rollback", "test"}


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def current(self) -> tuple[int, Settings]:
        row = self.db.query_one("SELECT version, settings_json FROM settings_versions ORDER BY version DESC LIMIT 1")
        if row is None:
            raise LookupError("설정이 초기화되지 않았습니다. `aifund setup`을 먼저 실행하세요.")
        return int(row["version"]), Settings.model_validate_json(row["settings_json"])

    def get(self, version: int) -> Settings:
        row = self.db.query_one("SELECT settings_json FROM settings_versions WHERE version=?", (version,))
        if row is None:
            raise LookupError(f"설정 버전 {version} 없음")
        return Settings.model_validate_json(row["settings_json"])

    def save(self, settings: Settings, actor: str, reason: str, source_file_hash: str | None = None) -> int:
        # AI 역할(research/review 등)은 actor로 허용되지 않는다 → AI는 설정을 바꿀 수 없다.
        if actor not in ALLOWED_ACTORS:
            raise PermissionError(f"설정 변경 권한이 없는 주체입니다: {actor}")
        Settings.model_validate(settings.model_dump())  # 재검증
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO settings_versions(created_at, actor, reason, settings_json, settings_hash, source_file_hash) "
                "VALUES (?,?,?,?,?,?)",
                (to_iso(utcnow()), actor, reason, settings.canonical_json(), settings.hash(), source_file_hash),
            )
            version = int(cur.lastrowid)
            c.execute(
                "INSERT INTO control_events(ts, actor, action, scope, detail_json) VALUES (?,?,?,?,?)",
                (to_iso(utcnow()), actor, "settings_saved", "settings", dumps({"version": version, "reason": reason})),
            )
        log.info("설정 버전 %s 저장 (%s: %s)", version, actor, reason)
        return version

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT version, created_at, actor, reason, settings_hash FROM settings_versions ORDER BY version DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def ensure_initialized(self, config_path: Path | None) -> tuple[int, Settings]:
        has_any = self.db.scalar("SELECT COUNT(*) FROM settings_versions") > 0
        if config_path is not None and config_path.exists():
            fh = file_hash(config_path)
            last_fh = self.db.scalar(
                "SELECT source_file_hash FROM settings_versions WHERE source_file_hash IS NOT NULL ORDER BY version DESC LIMIT 1"
            )
            if fh != last_fh:
                settings = load_settings_file(config_path)
                self.save(settings, "config_file", f"설정 파일 가져오기: {config_path.name}", source_file_hash=fh)
                return self.current()
        if not has_any:
            self.save(Settings(), "setup", "기본 엔지니어링 프리셋(검증된 투자조건 아님)")
        return self.current()


def env_overrides(settings: Settings) -> tuple[Settings, dict[str, str]]:
    """환경변수로 AI 공급자·모델을 고정할 수 있다(화면에는 '환경변수로 고정'으로 표시)."""
    locked: dict[str, str] = {}
    data = settings.model_dump()
    prov = os.environ.get("AIFUND_AI_PROVIDER")
    model = os.environ.get("AIFUND_AI_MODEL")
    if prov:
        data["ai"]["provider"] = prov
        locked["ai.provider"] = prov
    if model:
        data["ai"]["model"] = model
        locked["ai.model"] = model
    if not locked:
        return settings, locked
    return Settings.model_validate(data), locked
