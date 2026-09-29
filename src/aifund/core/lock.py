"""단일 서비스(중앙 주문 실행기) 보장을 위한 프로세스 잠금.

OS 수준 파일 잠금이라 프로세스가 비정상 종료되면 자동으로 풀린다.
macOS/Linux는 fcntl.flock, Windows는 msvcrt.locking을 사용한다.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from aifund.core.timeutil import to_iso, utcnow

if os.name == "nt":  # pragma: no cover - 플랫폼 분기
    import msvcrt
else:  # pragma: no cover
    import fcntl


class LockHeldError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path, purpose: str = "service") -> None:
        self.path = path
        self.purpose = purpose
        self._fh: Any = None

    def acquire(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            info = read_lock_info(self.path)
            raise LockHeldError(f"다른 프로세스가 이미 실행 중입니다: {info}") from exc
        self._fh = fh
        info = {"pid": os.getpid(), "host": socket.gethostname(), "since": to_iso(utcnow()), "purpose": self.purpose}
        # 잠금 바이트(0) 이후에 정보를 쓴다. Windows에서는 잠긴 바이트 영역에 쓰지 않는다.
        info_path = self.path.with_suffix(".info")
        info_path.write_text(json.dumps(info), encoding="utf-8")
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
            try:
                self.path.with_suffix(".info").unlink()
            except OSError:
                pass

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def read_lock_info(path: Path) -> dict[str, Any] | None:
    info_path = path.with_suffix(".info")
    try:
        return json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_locked(path: Path) -> bool:
    """잠금이 잡혀 있는지 비파괴적으로 확인."""
    try:
        probe = ProcessLock(path, purpose="probe").acquire()
    except LockHeldError:
        return True
    probe.release()
    return False
