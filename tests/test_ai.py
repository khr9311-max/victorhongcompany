"""AI 연동·예산 테스트. 실제 Anthropic API는 호출하지 않는다(가짜 HTTP 전송)."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal

import anthropic
import httpx2

from aifund.ai.budget import AIBudget, BudgetExceeded
from aifund.ai.provider import AnthropicProvider, DemoProvider, LLMUsage
from aifund.ai.schemas import ResearchReport
from aifund.config.settings import AISettings
from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock
from aifund.domain.models import FxRate
from aifund.service.context import build_context
from aifund.service.cycle import DecisionCycle
from helpers import make_env


def test_anthropic_request_shape_and_usage_iterations():
    seen = {}

    def handler(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        if req.url.path.endswith("/count_tokens"):
            seen["count"] = body
            return httpx2.Response(200, json={"input_tokens": 1234})
        seen["body"] = body
        seen["beta"] = req.headers.get("anthropic-beta")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": "{\"ok\": true}"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 50, "iterations": [
                {"type": "message", "model": "claude-opus-5", "input_tokens": 100, "output_tokens": 0},
                {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 100, "output_tokens": 50}]},
        })

    http_client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    p = AnthropicProvider(api_key="sk-ant-test", model="claude-opus-5", effort="medium", use_fallbacks=True, http_client=http_client)
    schema = anthropic.transform_schema(ResearchReport)
    n = asyncio.run(p.count_tokens("sys", "user", schema))
    assert n == 1234 and seen["count"]["output_config"]["format"]["type"] == "json_schema"
    res = asyncio.run(p.complete_json("sys", "user", schema, 4000))
    b = seen["body"]
    assert b["model"] == "claude-opus-5" and b["max_tokens"] == 4000
    assert b["output_config"]["format"]["type"] == "json_schema" and b["output_config"]["effort"] == "medium"
    assert b["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["beta"]
    assert "tools" not in b  # AI에는 도구(주문·쉘) 권한이 없다
    assert [u.model for u in res.usages] == ["claude-opus-5", "claude-opus-4-8"]
    assert res.text == "{\"ok\": true}"


def test_budget_reserve_settle_and_cap(tmp_path):
    env = make_env(tmp_path)
    from aifund.data.fx import FxService

    fx = FxService(env.db, clock=env.clock, provider="none")
    fx.record(FxRate("USDKRW", Decimal("1400"), env.clock.now(), env.clock.now(), "test"))
    s = AISettings(monthly_budget_krw=Decimal("300"))
    b = AIBudget(env.db, s, fx, env.clock)
    est = b.estimate_usd("claude-opus-5", 2000, 4000, fallbacks=True)
    assert est == (Decimal(2000) * 5 + Decimal(4000) * 25) / Decimal(1_000_000)
    bid = b.reserve(est, "r1")  # 0.11 USD × 1400 × 1.1 ≈ 169원
    try:
        b.reserve(est, "r2")
        raise AssertionError("예산 초과 예약이 허용되면 안 됨")
    except BudgetExceeded:
        pass
    usd, estimated = b.cost_usd([LLMUsage("claude-opus-5", 1000, 500)])
    assert usd == Decimal("0.0175") and not estimated
    krw = b.settle(bid, usd, estimated)
    assert krw == Decimal("24.50")
    assert b.month_usage().settled_krw == Decimal("24.50")
    usd2, est2 = b.cost_usd([LLMUsage("unknown-model", 1000, 0)])
    assert est2  # 요율 미확인 모델은 최대 요율 + 추정 표시


def test_pricing_missing_disables_paid_calls(home):
    ctx = build_context(mode_paths("internal_paper", home).ensure())
    ctx.settings.ai.model = "some-new-model"
    ctx.secrets = type(ctx.secrets)(**{**ctx.secrets.__dict__, "anthropic_api_key": "sk-ant-test"})
    ctx._provider = None
    ok, why = ctx.ai.availability()
    assert not ok and "요율 미설정" in why


def _demo_ctx(home, responder=None):
    clock = ManualClock(datetime(2026, 9, 28, 1, 0, tzinfo=UTC))  # KST 10:00 → 일일 연구 시각 이후
    ctx = build_context(mode_paths("offline_demo", home).ensure(), clock=clock)
    if responder is not None:
        ctx._provider = DemoProvider(responder)
    return ctx


def test_invalid_ai_output_holds_proposals(home):
    def bad(system, user):
        data = json.loads(user)
        if data.get("phase") != "research":
            return {}
        return {"market_summary": "x", "data_status": "x", "instrument_views": [
            {"instrument_id": "crypto:KRW-DOGE", "stance": "favorable", "summary": "12% 급등", "claims": [], "data_gaps": []}],
            "strategy_conditions": [], "proposals": [], "improvement_ideas": []}

    ctx = _demo_ctx(home, bad)
    cycle = DecisionCycle(ctx)
    asyncio.run(ctx.markets["crypto"].collector.refresh_instruments("crypto", ctx.settings.markets["crypto"].instruments))
    asyncio.run(cycle.run("crypto", trigger="manual"))
    snap = cycle.last_snapshots["crypto"]
    rid = asyncio.run(ctx.ai.research("crypto", snap, cycle.signals_payload(snap), "manual"))
    row = ctx.db.query_one("SELECT valid, validation_errors FROM ai_reports WHERE report_id=?", (rid,))
    assert row["valid"] == 0 and "미지원 종목" in row["validation_errors"]
    view = ctx.ai.view("crypto", "B", {})
    assert not view.available and not view.proposals


def test_setting_c_requires_reviewer_accept(home):
    ctx = _demo_ctx(home)
    cycle = DecisionCycle(ctx)
    asyncio.run(ctx.markets["crypto"].collector.refresh_instruments("crypto", ctx.settings.markets["crypto"].instruments))
    for _ in range(30):  # 24봉 수익률 계산에 충분한 데이터 확보
        ctx.clock.advance(hours=1)
    asyncio.run(cycle.run("crypto", trigger="manual"))
    snap = cycle.last_snapshots["crypto"]
    rid = asyncio.run(ctx.ai.research("crypto", snap, cycle.signals_payload(snap), "manual"))
    assert rid
    vb = ctx.ai.view("crypto", "B", {})
    vc_before = ctx.ai.view("crypto", "C", {})
    assert vb.available
    assert not vc_before.available and "검증 AI" in vc_before.reason  # 검증 전에는 C에서 AI 제안 보류
    asyncio.run(ctx.ai.review("crypto", rid))
    vc = ctx.ai.view("crypto", "C", {})
    assert vc.available
    # 데모 검증 AI는 비중 0.5 초과 제안을 거절한다 → 비중 0.3 제안은 C에서도 통과
    assert all(p.blocked_reason is None for p in vc.proposals if p.target_weight <= Decimal("0.5"))
    _ = timedelta
