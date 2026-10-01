"""설정 파일 자동 가져오기가 대시보드·CLI 변경을 덮어쓰지 않는지, 대시보드 폼의 새 항목이 저장되는지."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aifund.config.settings import Settings, load_settings_file
from aifund.config.store import merge_file_change
from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock
from aifund.service.context import build_context
from aifund.web.app import apply_form, create_app

PROFILE = Path(__file__).resolve().parents[1] / "config" / "paper.toml"


def _profile_copy(home: Path) -> Path:
    cfg = home / "config" / "paper.toml"
    cfg.write_text(PROFILE.read_text(encoding="utf-8"), encoding="utf-8")
    return cfg


def _save_web(ctx, **changes: dict) -> None:
    data = ctx.settings.model_dump(mode="json")
    for section, values in changes.items():
        for path, value in values.items():
            cur = data[section]
            *parents, leaf = path.split(".")
            for p in parents:
                cur = cur[p]
            cur[leaf] = value
    ctx.store_settings.save(Settings.model_validate(data), "web", "대시보드 변경")


def test_file_change_keeps_dashboard_edits(home):
    cfg = _profile_copy(home)
    paths = mode_paths("offline_demo", home)
    ctx = build_context(paths, config_path=cfg)
    _save_web(ctx, risk={"max_spread_pct": "0.4"}, notify={"webhook": True})
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("max_open_positions = 8", "max_open_positions = 6"), encoding="utf-8")

    again = build_context(paths, config_path=cfg)
    assert again.settings.risk.max_open_positions == 6  # 파일에서 바뀐 항목은 반영
    assert again.settings.risk.max_spread_pct == Decimal("0.4")  # 대시보드 변경은 유지
    assert again.settings.notify.webhook is True
    reason = again.store_settings.history(1)[0]["reason"]
    assert "유지" in reason and "risk.max_spread_pct" in reason and "notify.webhook" in reason

    # 파일이 그대로면 다시 가져오지 않는다
    assert build_context(paths, config_path=cfg).settings_version == again.settings_version


def test_merge_conflict_falls_back_to_whole_file(home):
    cfg = _profile_copy(home)
    paths = mode_paths("offline_demo", home)
    ctx = build_context(paths, config_path=cfg)
    _save_web(ctx, markets={"kr_stock.allocation_krw": "26000000", "us_stock.allocation_krw": "11000000"})
    text = cfg.read_text(encoding="utf-8")
    text = text.replace('allocation_krw = "1000000"', 'allocation_krw = "1500000"', 1)  # crypto
    text = text.replace('allocation_krw = "12000000"       # 미국', 'allocation_krw = "11500000"       # 미국')
    cfg.write_text(text, encoding="utf-8")

    again = build_context(paths, config_path=cfg)  # 합치면 배정 합계 3,900만 > 원금 3,800만 → 파일 전체 적용
    alloc = {m: again.settings.markets[m].allocation_krw for m in ("crypto", "kr_stock", "us_stock")}
    assert alloc == {"crypto": 1500000, "kr_stock": 25000000, "us_stock": 11500000}
    assert "합치기 실패" in again.store_settings.history(1)[0]["reason"]


def test_merge_ignores_form_string_numbers():
    merged, applied, kept = merge_file_change(
        base={"p": {"fast": 20, "bb_k": "2.0"}, "x": 1},
        new={"p": {"fast": 20, "bb_k": "2.0", "slow": 60}},
        current={"p": {"fast": "20", "bb_k": "2"}, "x": 1},
    )
    assert merged == {"p": {"fast": "20", "bb_k": "2", "slow": 60}}  # x는 파일에서 삭제됨
    assert applied == ["p.slow", "x"] and kept == []


def test_dashboard_form_edits_research_focus_and_news():
    s = load_settings_file(PROFILE)
    form = {"_bools": "1", "ai.enabled": "on", "ai.research_focus": "  반도체 우선  ",
            "m.kr_stock.present": "1", "m.kr_stock.enabled": "on", "n.kr_stock.queries": "삼성전자, , SK하이닉스",
            "m.crypto.present": "1", "m.crypto.enabled": "on", "n.crypto.queries": ""}
    s2 = apply_form(s, form)
    assert s2.ai.research_focus == "반도체 우선"
    assert s2.news.naver_queries["kr_stock"] == ["삼성전자", "SK하이닉스"]
    assert "crypto" not in s2.news.naver_queries
    assert s2.news.naver_enabled is False  # 체크 해제로 저장
    assert apply_form(s2, {"news.naver_enabled": "on"}).news.naver_enabled is True
    with pytest.raises(ValidationError, match="최대 10개"):
        apply_form(s, {"m.kr_stock.present": "1", "m.kr_stock.enabled": "on",
                       "n.kr_stock.queries": ",".join(f"검색어{i}" for i in range(11))})


def test_settings_page_shows_new_fields(home):
    ctx = build_context(mode_paths("offline_demo", home), config_path=PROFILE,
                        clock=ManualClock(datetime(2026, 9, 29, 14, 0, tzinfo=UTC)))
    page = TestClient(create_app(ctx, None), base_url="http://127.0.0.1:8765").get("/settings")
    assert page.status_code == 200
    assert 'name="ai.research_focus"' in page.text and "HBM" in page.text
    assert 'name="n.us_stock.queries"' in page.text and 'name="news.naver_enabled"' in page.text
