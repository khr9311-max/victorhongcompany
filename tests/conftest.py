import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 테스트 중에는 실제 비밀값을 절대 읽지 않는다
for k in list(os.environ):
    if k.startswith(("UPBIT_", "KIS_", "ANTHROPIC_", "AIFUND_", "DART_")):
        del os.environ[k]


@pytest.fixture(autouse=True)
def _restore_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFUND_HOME", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "src" / "aifund").mkdir(parents=True)
    return tmp_path
