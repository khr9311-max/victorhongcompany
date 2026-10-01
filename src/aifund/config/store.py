"""설정 버전 저장소. 모든 변경은 버전·작성자·사유와 함께 기록된다.

설정 파일(config/paper.toml 또는 config/config.toml)이 바뀌면 다음 시작 때 자동으로 가져온다.
이때 '직전에 가져온 파일 내용 → 현재 파일'에서 바뀐 항목만 현재 설정에 반영하므로,
대시보드·CLI로 바꾼 다른 항목은 유지된다. 합친 결과가 검증을 통과하지 못하면 파일 전체를 적용한다.
`aifund settings import <파일>`은 명시적 전체 가져오기다.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aifund.config.settings import Settings, file_hash, load_settings_file
from aifund.core.timeutil import to_iso, utcnow
from aifund.db.database import Database, dumps

log = logging.getLogger(__name__)

ALLOWED_ACTORS = {"setup", "config_file", "cli", "web", "candidate_promotion", "candidate_rollback", "test"}

_MISSING = object()


def _flatten(d: dict[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    out: dict[tuple[str, ...], Any] = {}
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out.update(_flatten(v, prefix + (k,)))
        else:
            out[prefix + (k,)] = v
    return out


def _same(a: Any, b: Any) -> bool:
    """값 비교. 대시보드 폼은 숫자를 문자열로 보내므로 20과 "20", "0.30"과 "0.3"은 같은 값으로 본다."""
    if a == b:
        return True
    scalar = (int, float, str)
    if isinstance(a, scalar) and isinstance(b, scalar) and not isinstance(a, bool) and not isinstance(b, bool):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except InvalidOperation:
            return False
    return False


def _delete(d: dict[str, Any], path: tuple[str, ...]) -> None:
    for p in path[:-1]:
        d = d.get(p)  # type: ignore[assignment]
        if not isinstance(d, dict):
            return
    d.pop(path[-1], None)


def _put(d: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for p in path[:-1]:
        if not isinstance(d.get(p), dict):
            d[p] = {}
        d = d[p]
    d[path[-1]] = copy.deepcopy(value)


def merge_file_change(base: dict[str, Any], new: dict[str, Any], current: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """base(직전 파일) → new(현재 파일)에서 바뀐 항목만 current에 적용한다.

    반환: (합친 설정, 파일에서 반영한 항목, 파일과 다르게 유지된 대시보드·CLI 항목)."""
    fb, fn = _flatten(base), _flatten(new)
    changed = [p for p, v in fn.items() if not _same(fb.get(p, _MISSING), v)]
    removed = [p for p in fb if p not in fn]
    merged = copy.deepcopy(current)
    for p in removed:
        _delete(merged, p)
    for p in changed:
        _put(merged, p, fn[p])
    fm = _flatten(merged)
    kept = sorted({".".join(p) for p in set(fm) | set(fn) if not _same(fm.get(p, _MISSING), fn.get(p, _MISSING))})
    return merged, sorted(".".join(p) for p in changed + removed), kept


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
            last = self.db.query_one(
                "SELECT source_file_hash, settings_json FROM settings_versions WHERE source_file_hash IS NOT NULL "
                "ORDER BY version DESC LIMIT 1"
            )
            if last is None or fh != last["source_file_hash"]:
                settings = load_settings_file(config_path)
                reason = f"설정 파일 가져오기: {config_path.name}"
                if last is not None:
                    settings, reason = self._merge_with_current(config_path.name, json.loads(last["settings_json"]), settings)
                self.save(settings, "config_file", reason, source_file_hash=fh)
                return self.current()
        if not has_any:
            self.save(Settings(), "setup", "기본 엔지니어링 프리셋(검증된 투자조건 아님)")
        return self.current()

    def _merge_with_current(self, name: str, base: dict[str, Any], new: Settings) -> tuple[Settings, str]:
        try:
            _, current = self.current()
        except ValidationError:  # 저장된 설정이 현재 코드 검증을 통과하지 못하면 파일로 복구
            log.warning("저장된 설정을 읽을 수 없어 설정 파일 전체를 적용합니다: %s", name)
            return new, f"설정 파일 가져오기: {name} (저장된 설정 검증 실패로 전체 적용)"
        merged, applied, kept = merge_file_change(base, new.model_dump(mode="json"), current.model_dump(mode="json"))
        if not kept:
            return new, f"설정 파일 가져오기: {name}"
        try:
            settings = Settings.model_validate(merged)
        except ValidationError as exc:
            log.warning("설정 파일 변경과 대시보드·CLI 변경을 합칠 수 없어 파일 전체를 적용합니다(덮어쓴 항목: %s): %s",
                        ", ".join(kept), exc.errors()[0].get("msg"))
            return new, f"설정 파일 가져오기: {name} (합치기 실패로 전체 적용, 덮어쓴 항목: {', '.join(kept[:10])})"
        log.info("설정 파일 변경 %s개 반영, 대시보드·CLI 변경 유지: %s", len(applied), ", ".join(kept))
        return settings, f"설정 파일 가져오기: {name} (변경 {len(applied)}개 반영, 유지: {', '.join(kept[:10])})"


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
