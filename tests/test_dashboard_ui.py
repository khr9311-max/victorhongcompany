"""대시보드 표시 부품: 차트 데이터, 자산 구성 합계, 상태 요약, 안전한 JSON 삽입, 상대 시간."""

import asyncio
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock
from aifund.service.context import build_context
from aifund.service.simulate import simulate
from aifund.web import charts, views
from aifund.web.app import ago_text, create_app, krw_short


def test_dashboard_charts_composition_and_pages(home):
    ctx = build_context(mode_paths("offline_demo", home).ensure(), clock=ManualClock(datetime.now(UTC) - timedelta(hours=30)))
    asyncio.run(simulate(ctx, 24))

    eq = charts.equity_chart(ctx)
    points = eq["series"][0]["points"]
    assert 2 <= len(points) <= charts.MAX_POINTS + 2 and eq["reference"]["value"] == 300000
    assert 0 < len(eq["table"]) <= charts.TABLE_ROWS
    assert float(eq["table"][-1]["values"][0]) == points[-1][1]  # 표의 마지막 행 = 그래프 끝점

    cmp = charts.compare_chart(ctx)
    assert [s["key"] for s in cmp["series"]] == ["shadow_A", "shadow_B", "shadow_C", "baseline_bh"]
    assert [s["slot"] for s in cmp["series"]] == [1, 2, 3, 4]  # 장부마다 고정 색
    assert all(p[1] > -100 for s in cmp["series"] for p in s["points"])

    comp = views.composition(views.capital(ctx))
    assert sum(p["value"] for p in comp["parts"]) == comp["total"]  # 현금 + 시장별 보유 = 총자산

    items = views.health(ctx, None, views.market_rows(ctx, None), views.capital(ctx))
    assert items[0]["level"] == "warning" and "대시보드만" in items[0]["text"]

    client = TestClient(create_app(ctx, None), base_url="http://127.0.0.1:8765")
    home_page = client.get("/").text
    assert 'class="chart-data"' in home_page and "자산 구성" in home_page and "상태 요약" in home_page
    for path in ("/strategies", "/research", "/orders", "/control", "/settings", "/login"):
        assert client.get(path).status_code == 200, path
    assert "장부별 수익률 추이" in client.get("/strategies").text


def test_chart_json_cannot_break_out_of_script():
    out = charts.spec_json({"series": [{"label": "</script><script>alert(1)</script>", "points": []}], "table": [object()]})
    assert "</script" not in out and "table" not in out


def test_relative_time_and_short_krw():
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert ago_text(now - timedelta(seconds=10), now) == "방금"
    assert ago_text(now - timedelta(minutes=5, seconds=20), now) == "5분 전"
    assert ago_text(now + timedelta(hours=3), now) == "3시간 후"
    assert ago_text(None, now) == "-"
    assert krw_short(5_012_300) == "501.2만원" and krw_short(-250_000_000) == "-2.50억원"
