"""AI 직원 확장: 애널리스트 → 수석 연구원 → 검증 AI → 리스크 매니저 흐름, 메모 전달·제외, C 설정 리스크 판정, 비용 배분, 화면."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from aifund.ai.demo_responder import demo_responder
from aifund.ai.evidence import Bundle
from aifund.ai.gemini import GeminiProvider
from aifund.ai.provider import DemoProvider
from aifund.ai.service import AIService
from aifund.ai.team import ROSTER, setting_roles
from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock, to_iso
from aifund.db.database import dumps
from aifund.service.context import build_context
from aifund.service.cycle import DecisionCycle
from aifund.web.app import apply_form, create_app

PROFILE = Path(__file__).resolve().parents[1] / "config" / "paper.toml"
RISK_VETO = {"summary": "x", "portfolio_concerns": [], "verdicts": [
    {"proposal_ref": "P1", "verdict": "reject", "max_weight": 0.4,
     "reasons": [{"category": "cross_market_concentration", "detail": "같은 테마 집중", "source_ids": ["pf:book"]}]}]}


def _responder(overrides=None):
    """데모 응답 + 수석 연구원은 항상 P1 매수(0.4)·P2 매도 제안. overrides[phase](data)로 특정 직원 응답을 바꾼다."""
    def responder(system, user):
        data = json.loads(user)
        phase = data.get("phase")
        if overrides and phase in overrides:
            return overrides[phase](data)
        out = demo_responder(system, user)
        if phase == "research":
            facts = data["price_facts"]
            base = {"counterarguments": ["단기 과열"], "invalidation": "추세 이탈 시", "horizon_hours": 24, "cost_considered": "수수료 반영"}
            out["proposals"] = [
                {"proposal_ref": "P1", "instrument_id": facts[0]["instrument_id"], "action": "buy", "target_weight": 0.4,
                 "rationale": "상대 강세", "source_ids": [facts[0]["id"]], **base},
                {"proposal_ref": "P2", "instrument_id": facts[1]["instrument_id"], "action": "sell", "target_weight": 0,
                 "rationale": "약세", "source_ids": [facts[1]["id"]], **base},
            ]
        return out
    return responder


def _desk(home, overrides=None, news=2):
    ctx = build_context(mode_paths("offline_demo", home).ensure(), clock=ManualClock(datetime(2026, 9, 28, 1, tzinfo=UTC)))
    ctx._provider = DemoProvider(_responder(overrides))
    cycle = DecisionCycle(ctx)
    asyncio.run(ctx.markets["crypto"].collector.refresh_instruments("crypto", ctx.settings.markets["crypto"].instruments))
    asyncio.run(cycle.run("crypto", trigger="manual"))
    now = to_iso(ctx.clock.now())
    for i in range(news):
        ctx.db.execute("INSERT INTO sources(source_id, kind, feed, title, url, published_at, fetched_at, market, instruments_json, summary) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", (f"news:t{i}", "news", "test", f"기사 {i}", None, now, now, "crypto", "[]", "요약"))
    snap = cycle.last_snapshots["crypto"]
    return ctx, lambda: asyncio.run(ctx.ai.run_desk("crypto", snap, cycle.signals_payload(snap), "manual"))


def _calls(ctx):
    return [json.loads(user) for _, user in ctx._provider.calls]


def _proposal(view, ref_iid):
    return next(p for p in view.proposals if p.instrument_id == ref_iid)


def test_full_desk_runs_each_employee_once_in_order(home):
    ctx, run = _desk(home)
    rid = run()
    calls = _calls(ctx)
    assert [c["phase"] for c in calls] == ["news_analyst", "quant_analyst", "research", "independent", "review", "risk_review"]
    news_in, quant_in, research_in, independent_in, review_in, risk_in = calls
    # 모든 직원이 같은 시각 기준을 본다: 가격은 마지막 완성봉, 뉴스는 수집 시각 전 공개분(밤사이 뉴스를 미래정보로 버리지 않게)
    assert all("created_at" in c and "created_at(자료 수집·판단 시각)" in c["time_rules"] for c in calls)
    assert "price_facts" not in news_in and news_in["instrument_names"]  # 뉴스 애널리스트는 뉴스만
    assert "sources_UNTRUSTED_DATA" not in quant_in  # 퀀트 애널리스트는 숫자만
    assert set(research_in["analyst_memos"]) == {"news_analyst", "quant_analyst"}
    assert "analyst_memos" not in independent_in and "research_report" not in independent_in  # 독립 평가는 원자료만
    assert set(review_in["analyst_memos"]) == {"news_analyst", "quant_analyst"}
    assert [p["proposal_ref"] for p in risk_in["proposals_under_review"]] == ["P1"]  # 매도 제안은 리스크 판정 대상 아님
    items = {x["id"] for x in risk_in["portfolio"]["items"]}
    assert {"pf:book", "pf:limits", "pf:risk_state", "pf:ai_sleeve:crypto"} <= items
    assert risk_in["portfolio"]["book_id"] == "shadow_C"  # 운용 설정이 A면 판정이 적용되는 비교 C 장부를 본다
    assert ctx._provider.efforts == ["medium", "medium", None, None, None, None]  # 애널리스트만 낮은 추론 강도, 나머지는 기본(effort)
    children = {r["role"]: r["valid"] for r in ctx.db.query("SELECT role, valid FROM ai_reports WHERE parent_report_id=?", (rid,))}
    assert children == {"news_analyst": 1, "quant_analyst": 1, "review_independent": 1, "review": 1, "risk_manager": 1}
    roles = {r[0] for r in ctx.db.query("SELECT role FROM ai_runs")}
    assert roles == {"news_analyst", "quant_analyst", "research", "review_independent", "review", "risk_manager"}

    c = ctx.ai.view("crypto", "C", {})
    p1, p2 = _proposal(c, calls[2]["price_facts"][0]["instrument_id"]), _proposal(c, calls[2]["price_facts"][1]["instrument_id"])
    assert c.available and c.risk_report_id
    assert p1.blocked_reason is None and p1.target_weight == Decimal("0.25") and "리스크 매니저 비중 축소" in p1.rationale
    assert p2.blocked_reason is None and p2.action.value == "sell"
    b = ctx.ai.view("crypto", "B", {})  # B(연구팀만)는 검증팀 판정을 받지 않는다
    assert _proposal(b, p1.instrument_id).target_weight == Decimal("0.4")


def test_risk_manager_reject_missing_invalid_and_off(home):
    ctx, run = _desk(home, {"risk_review": lambda d: RISK_VETO})
    run()
    iid = _calls(ctx)[2]["price_facts"][0]["instrument_id"]
    p1 = _proposal(ctx.ai.view("crypto", "C", {}), iid)
    assert p1.blocked_reason.startswith("리스크 매니저 거절") and "같은 테마 집중" in p1.blocked_reason

    ctx.settings.ai.risk_manager_enabled = False  # 끄면 C도 검증 AI 판정만 적용
    p1 = _proposal(ctx.ai.view("crypto", "C", {}), iid)
    assert p1.blocked_reason is None and p1.target_weight == Decimal("0.4")


def test_risk_manager_failure_holds_c_buys_only(home):
    ctx, run = _desk(home, {"risk_review": lambda d: {}})  # 스키마 위반 → 보고서 없음
    run()
    view = ctx.ai.view("crypto", "C", {})
    facts = _calls(ctx)[2]["price_facts"]
    assert _proposal(view, facts[0]["instrument_id"]).blocked_reason.startswith("리스크 매니저 검토 없음")
    assert _proposal(view, facts[1]["instrument_id"]).blocked_reason is None  # 위험을 줄이는 매도는 막지 않는다

    bad = {**RISK_VETO, "verdicts": [{**RISK_VETO["verdicts"][0], "proposal_ref": "P9"}]}  # 없는 제안 판정 → 검증 실패
    ctx2, run2 = _desk(home / "b", {"risk_review": lambda d: bad})
    run2()
    iid = _calls(ctx2)[2]["price_facts"][0]["instrument_id"]
    assert _proposal(ctx2.ai.view("crypto", "C", {}), iid).blocked_reason.startswith("리스크 매니저 결과 검증 실패")


def test_invalid_analyst_memo_is_excluded_and_research_continues(home):
    fake_news = {"summary": "s", "coverage_gaps": [], "events": [
        {"headline": "지어낸 사건", "instrument_ids": [], "category": "other", "direction": "unclear", "materiality": "high",
         "verification": "reported", "source_ids": ["news:없는기사"], "note": ""}]}
    ctx, run = _desk(home, {"news_analyst": lambda d: fake_news})
    rid = run()
    research_in = _calls(ctx)[2]
    assert set(research_in["analyst_memos"]) == {"quant_analyst"}
    assert any("뉴스·공시 애널리스트 메모는 검증 실패로 제외" in x for x in research_in["data_status"])
    row = ctx.db.query_one("SELECT valid, validation_errors FROM ai_reports WHERE role='news_analyst'")
    assert row["valid"] == 0 and "번들에 없는 source_id" in row["validation_errors"]
    assert ctx.ai._valid(rid)  # 수석 연구원 보고서는 원자료로 검증되어 그대로 쓰인다


def test_analysts_off_or_no_news_keeps_original_flow(home):
    ctx, run = _desk(home, news=0)
    run()
    assert [c["phase"] for c in _calls(ctx)][:2] == ["quant_analyst", "research"]  # 뉴스가 없으면 뉴스 애널리스트는 쉰다
    assert any("검토할 뉴스·공시 없음" in x for x in _calls(ctx)[1]["data_status"])

    ctx2, run2 = _desk(home / "b")
    ctx2.settings.ai.news_analyst_enabled = False
    ctx2.settings.ai.quant_analyst_enabled = False
    run2()
    calls = _calls(ctx2)
    assert [c["phase"] for c in calls] == ["research", "independent", "review", "risk_review"]
    assert "analyst_memos" not in calls[0]


def test_gemini_thinking_level_per_call():
    levels = []

    def handler(req):
        levels.append(json.loads(req.content)["generationConfig"].get("thinkingConfig", {}).get("thinkingLevel"))
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}],
                                          "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            p = GeminiProvider(api_key="k", model="gemini-3.8-flash", thinking_level="high", client=c)
            await p.complete_json("s", "u", {}, 1000)
            await p.complete_json("s", "u", {}, 1000, effort="medium")
    asyncio.run(run())
    assert levels == ["high", "medium"]


def test_unavailable_ai_records_single_skip(home):
    ctx, run = _desk(home)
    ctx.settings.ai.enabled = False
    assert run() is None
    assert [(r["role"], r["status"]) for r in ctx.db.query("SELECT role, status FROM ai_runs")] == [("research", "skipped")]


def test_memo_events_citing_trimmed_news_are_dropped():
    now = datetime(2026, 9, 28, tzinfo=UTC)
    sources = [{"id": f"news:{i}", "kind": "news", "title": "가" * 300, "summary": ""} for i in range(10)]
    b = Bundle("snap", "crypto", now, now, ["crypto:KRW-BTC"], [{"id": "px:1", "instrument_id": "crypto:KRW-BTC"}], [], sources, {})
    event = {"headline": "h", "instrument_ids": [], "category": "other", "direction": "unclear", "materiality": "low",
             "verification": "reported", "note": ""}
    memos = {"news_analyst": {"summary": "s", "coverage_gaps": [], "events": [
        {**event, "source_ids": ["news:9"]}, {**event, "source_ids": ["news:0"]}]}}
    text = AIService._research_text(b, 2500, {"phase": "research"}, memos)
    d = json.loads(text)
    kept = {x["id"] for x in d["sources_UNTRUSTED_DATA"]}
    assert "news:9" not in kept and "news:0" in kept
    assert [e["source_ids"] for e in d["analyst_memos"]["news_analyst"]["events"]] == [["news:0"]]


def test_cost_attribution_by_team(home):
    ctx = build_context(mode_paths("offline_demo", home).ensure())
    for i, e in enumerate(r for m in ROSTER for r in m.roles):
        ctx.db.execute("INSERT INTO ai_runs(run_id, role, provider, model, prompt_version, started_at, status, cost_krw) "
                       "VALUES (?,?,?,?,?,?,?,?)", (f"r{i}", e, "demo", "demo", "v", to_iso(ctx.clock.now()), "ok", "10"))
    assert setting_roles("B") == ("news_analyst", "quant_analyst", "research")
    assert ctx.ai_cost_for_setting("A") == 0
    assert ctx.ai_cost_for_setting("B") == Decimal("30")
    assert ctx.ai_cost_for_setting("C") == Decimal("60")  # + 검증 AI 2단계 + 리스크 매니저, 주간 전략 연구원 제외


def test_portfolio_view_shows_other_markets_ai_proposals(home):
    ctx = build_context(mode_paths("offline_demo", home), config_path=PROFILE,
                        clock=ManualClock(datetime(2026, 9, 28, 1, tzinfo=UTC)))
    now = ctx.clock.now()
    report = {"report": {"proposals": [{"proposal_ref": "P1", "instrument_id": "us_stock:NASD:NVDA", "action": "buy",
                                        "target_weight": 0.5, "rationale": "AI 수요"}]}, "bundle": {}}
    ctx.db.execute("INSERT INTO ai_reports(report_id, run_id, role, market, created_at, expires_at, valid, validation_errors, report_json) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", ("rep-us", "run-us", "research", "us_stock", to_iso(now),
                                                  to_iso(now + timedelta(hours=10)), 1, "[]", dumps(report)))
    pf = ctx.ai_portfolio("kr_stock")
    items = {x["id"]: x for x in pf["items"]}
    assert pf["book_id"] == "operating"  # paper.toml은 운용 설정 C
    assert items["pf:ai:us_stock:P1"]["instrument_id"] == "us_stock:NASD:NVDA"
    assert items["pf:limits"]["max_open_positions"] == 8 and "pf:ai_sleeve:kr_stock" in items


def test_research_page_shows_team_and_settings_toggle(home):
    ctx, run = _desk(home)
    run()
    client = TestClient(create_app(ctx, None), base_url="http://127.0.0.1:8765")
    page = client.get("/research").text
    assert "AI 직원" in page and "연구 세트" in page and "비중 축소" in page
    for e in ROSTER:
        assert e.title in page
    settings_page = client.get("/settings").text
    assert 'name="ai.risk_manager_enabled"' in settings_page and 'name="ai.analyst_effort"' in settings_page
    s = apply_form(ctx.settings, {"_bools": "1", "ai.enabled": "on", "ai.risk_manager_enabled": "on", "ai.analyst_effort": "low"})
    assert not s.ai.news_analyst_enabled and not s.ai.quant_analyst_enabled and s.ai.risk_manager_enabled
    assert s.ai.analyst_effort == "low"
