"""검증 1: API 키 없이 설치·offline demo·대시보드가 실행되는가."""

import asyncio
import shutil
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from aifund.core.paths import mode_paths
from aifund.core.secrets import load_mode_secrets
from aifund.core.timeutil import UTC, ManualClock
from aifund.service.context import build_context
from aifund.service.simulate import simulate
from aifund.web.app import create_app


def test_offline_demo_without_keys(home):
    secrets = load_mode_secrets("offline_demo")
    assert secrets.upbit is None and secrets.anthropic_api_key is None and secrets.kis_trade is None
    ctx = build_context(mode_paths("offline_demo", home).ensure(), clock=ManualClock(datetime.now(UTC) - timedelta(hours=40)))
    summary = asyncio.run(simulate(ctx, 36))
    assert summary["cycles"] == 36
    assert summary["notes"] == []  # 원장 검증 통과
    assert summary["research"] >= 1  # 데모 AI(무료, [데모] 표시)
    for b in ctx.book_ids():
        assert ctx.ledger.verify(b) == []
    rep = ctx.db.query_one("SELECT report_json FROM ai_reports WHERE role='research'")
    assert "[데모]" in rep["report_json"]
    client = TestClient(create_app(ctx, None), base_url="http://127.0.0.1:8765")
    for path in ("/", "/strategies", "/research", "/orders", "/control", "/settings", "/api/status", "/login"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    assert "가짜(합성) 데이터" in client.get("/").text


def test_setup_creates_env_token_and_db(home):
    from aifund import cli

    real_root = __import__("pathlib").Path(__file__).resolve().parents[1]
    shutil.copy(real_root / ".env.example", home / ".env.example")
    shutil.copy(real_root / "config" / "config.example.toml", home / "config" / "config.example.toml")
    rc = cli.main(["--mode", "offline_demo", "setup"])
    assert rc == 0
    env = (home / ".env").read_text(encoding="utf-8")
    token_line = [ln for ln in env.splitlines() if ln.startswith("AIFUND_ADMIN_TOKEN=")][0]
    assert len(token_line.split("=", 1)[1]) >= 32
    assert (home / "var" / "offline_demo" / "aifund.sqlite3").exists()
    # 다른 모드 DB는 만들어지지 않는다(모드 격리)
    assert not (home / "var" / "live").exists()
