"""설정 파일 자동 가져오기가 대시보드·CLI 변경을 덮어쓰지 않는지, 대시보드 폼의 새 항목이 저장되는지."""

from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aifund.config.settings import Settings, load_settings_file
from aifund.config.store import merge_file_change
from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock
from aifund.lab.catalog import find
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


class _FormFields(HTMLParser):
    """설정 화면 저장 폼이 그대로 보내는 값(체크된 체크박스·선택된 옵션 포함)."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self._in_form = False
        self._select: str | None = None
        self._option: dict[str, str | None] | None = None
        self._textarea: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self._in_form = a.get("action") == "/settings/save"
        if not self._in_form:
            return
        if tag == "input" and a.get("name"):
            if a.get("type") != "checkbox":
                self.fields[a["name"]] = a.get("value") or ""
            elif "checked" in a:
                self.fields[a["name"]] = "on"
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select:
            self._option = {"value": a.get("value"), "text": "", "selected": "selected" in a}
        elif tag == "textarea":
            self._textarea = a.get("name")
            self.fields[self._textarea] = ""

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option["text"] += data
        elif self._textarea:
            self.fields[self._textarea] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False
        elif tag == "option" and self._option is not None:
            o, self._option = self._option, None
            if o["selected"] or self._select not in self.fields:
                self.fields[self._select] = o["value"] if o["value"] is not None else o["text"]
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None


def test_settings_page_lists_lab_strategies_and_round_trips(home):
    ctx = build_context(mode_paths("offline_demo", home), config_path=PROFILE,
                        clock=ManualClock(datetime(2026, 9, 29, 14, 0, tzinfo=UTC)))
    html = TestClient(create_app(ctx, None), base_url="http://127.0.0.1:8765").get("/settings").text
    # 설정에 켜 둔 연구소 전략과 시장별 슬리브가 보인다
    for vid in ("rs_top", "high_52w.atr_trail", "trendy_kangaroo.split"):
        assert f'name="lab.{vid}.present"' in html and f"연구소 {find(vid).label}" in html
    assert 'name="msleeve.kr_stock.lab:rs_top" value="0.2"' in html and 'name="sleeve.trend_sma" value="0.4"' in html
    # 카탈로그(아직 안 쓰는 전략)는 '추가' 체크로만 들어가고, 코인 60분봉에서 1년 데이터가 필요한 전략은 안내한다
    assert 'name="lab.ma_cross.atr_trail.enabled"' in html and 'name="lab.ma_cross.atr_trail.present"' not in html
    assert "완성봉 8,762개 필요 — 이 봉 간격에서는 쓸 수 없음" in html
    assert 'name="lab.big_belt.zone.m.crypto"' not in html  # 빅 벨트는 갭이 있는 주식만

    parser = _FormFields()
    parser.feed(html)
    saved = apply_form(ctx.settings, parser.fields)  # 아무것도 안 바꾸고 저장하면 설정이 그대로다
    before, after = ctx.settings.model_dump(mode="json"), saved.model_dump(mode="json")
    for d in (before, after):  # 봇 파라미터는 폼이 문자열로 보낸다(값은 같음, 설정 합치기도 같은 값으로 봄)
        for sid in ("trend_sma", "mean_reversion"):
            d["strategies"][sid]["params"] = {k: str(v) for k, v in d["strategies"][sid]["params"].items()}
    assert after == before


def test_dashboard_form_edits_lab_strategies_and_sleeves():
    s = load_settings_file(PROFILE)
    off = apply_form(s, {"lab.trendy_kangaroo.split.present": "1", "lab.trendy_kangaroo.split.m.kr_stock": "on"})
    assert off.strategies.lab["trendy_kangaroo.split"].enabled is False
    assert off.strategies.lab["trendy_kangaroo.split"].markets == ["kr_stock"]

    added = apply_form(s, {"lab.ma_cross.atr_trail.enabled": "on", "lab.ma_cross.atr_trail.m.kr_stock": "on",
                           "msleeve.kr_stock.lab:ma_cross.atr_trail": "0.1", "msleeve.kr_stock.trend_sma": "0.05",
                           "msleeve.us_stock.mean_reversion": "", "lab.dual_momentum.m.kr_stock": "on"})
    assert added.strategies.lab["ma_cross.atr_trail"].enabled and added.strategies.lab["ma_cross.atr_trail"].markets == ["kr_stock"]
    kr = added.strategies.sleeves_for("kr_stock")
    assert kr["lab:ma_cross.atr_trail"] == Decimal("0.1") and kr["trend_sma"] == Decimal("0.05")
    assert "mean_reversion" not in added.strategies.sleeves_for("us_stock")  # 칸을 비우면 그 전략 몫 없음
    assert "dual_momentum" not in added.strategies.lab  # '추가'를 체크하지 않은 카탈로그 전략은 그대로
    assert added.strategies.sleeves == s.strategies.sleeves

    for form, msg in (
        ({"msleeve.kr_stock.lab:dual_momentum": "0.1"}, "추가"),
        ({"lab.dual_momentum.enabled": "on"}, "시장"),
        ({"sleeve.lab:nope": "0.1"}, "알 수 없는 슬리브"),
        ({"msleeve.kr_stock.trend_sma": "0.5"}, "1을 넘을 수 없습니다"),
        ({"lab.big_belt.zone.enabled": "on", "lab.big_belt.zone.m.crypto": "on"}, "쓸 수 없습니다"),
    ):
        with pytest.raises(ValueError, match=msg):
            apply_form(s, form)
