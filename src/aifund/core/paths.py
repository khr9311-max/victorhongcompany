"""모드별로 격리된 데이터 경로.

offline_demo / internal_paper / broker_sandbox / live 는 DB·원장·캐시·로그·토큰 캐시를
서로 다른 디렉터리에 둔다. 모드를 바꿔도 잔고·성과가 섞이지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MODES: tuple[str, ...] = ("offline_demo", "internal_paper", "broker_sandbox", "live")

MODE_LABELS = {
    "offline_demo": "오프라인 데모(가짜 데이터)",
    "internal_paper": "내부 모의체결(실시세)",
    "broker_sandbox": "증권사 공식 모의투자",
    "live": "실거래",
}


def project_root() -> Path:
    env = os.environ.get("AIFUND_HOME")
    if env:
        return Path(env).resolve()
    # src/aifund/core/paths.py -> 프로젝트 루트
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ModePaths:
    root: Path
    mode: str

    @property
    def data_dir(self) -> Path:
        return self.root / "var" / self.mode

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aifund.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "run"

    @property
    def lock_path(self) -> Path:
        return self.run_dir / "service.lock"

    @property
    def heartbeat_path(self) -> Path:
        return self.run_dir / "heartbeat.json"

    @property
    def backup_dir(self) -> Path:
        return self.root / "backups" / self.mode

    def ensure(self) -> "ModePaths":
        for d in (self.data_dir, self.log_dir, self.cache_dir, self.secrets_dir, self.run_dir, self.backup_dir):
            d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.secrets_dir, 0o700)
        except OSError:  # Windows 등에서는 무시
            pass
        return self


def mode_paths(mode: str, root: Path | None = None) -> ModePaths:
    if mode not in MODES:
        raise ValueError(f"알 수 없는 모드: {mode} (가능: {', '.join(MODES)})")
    return ModePaths(root=(root or project_root()), mode=mode)
