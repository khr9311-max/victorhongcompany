"""로그 설정. 파일 로그는 크기 기반으로 순환하고, 비밀값은 마스킹한다."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from aifund.core.secrets import RedactingFilter

_CONFIGURED = False


def setup_logging(log_dir: Path | None, level: str = "INFO", console: bool = True) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    redactor = RedactingFilter()
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "aifund.log", maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(redactor)
        root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        ch.addFilter(redactor)
        root.addHandler(ch)
    for noisy in ("httpx", "httpcore", "httpx2", "anthropic", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True
