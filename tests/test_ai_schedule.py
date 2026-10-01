"""정기 연구 일정(코인 N시간마다·주식 개장 전), 예고된 요율 변경, Gemini 추론 강도."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from aifund.ai.gemini import GeminiProvider, thinking_level_for
from aifund.ai.provider import LLMUsage
from aifund.core.paths import mode_paths
from aifund.core.timeutil import KST, UTC, ManualClock, to_iso
from aifund.service.context import build_context

PROFILE = Path(__file__).resolve().parents[1] / "config" / "paper.toml"


def _ctx(home, when: datetime):
    return build_context(mode_paths("offline_demo", home), config_path=PROFILE, clock=ManualClock(when))


def _ran(ctx, market: str, status: str = "ok") -> None:
    ctx.db.execute("INSERT INTO ai_runs(run_id, role, market, trigger, provider, model, prompt_version, started_at, status) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (f"r{ctx.clock.now().timestamp()}{market}", "research", market, "scheduled",
                                                  "demo", "demo", "research_v1", to_iso(ctx.clock.now()), status))


def test_crypto_research_every_six_hours(home):
    ctx = _ctx(home, datetime(2026, 10, 7, 14, 0, tzinfo=KST))
    start, length = ctx.ai.research_slot("crypto")
    assert start.astimezone(KST).strftime("%H:%M") == "08:50" and length == timedelta(hours=6)
    assert ctx.ai.due_research("crypto")
    _ran(ctx, "crypto")
    assert not ctx.ai.due_research("crypto")
    ctx.clock.set(datetime(2026, 10, 7, 14, 55, tzinfo=KST))  # 14:50 구간 시작
    assert ctx.ai.due_research("crypto")
    ctx.clock.set(datetime(2026, 10, 8, 3, 0, tzinfo=KST))  # 자정 넘어 02:50 구간
    assert ctx.ai.research_slot("crypto")[0].astimezone(KST).strftime("%m-%d %H:%M") == "10-08 02:50"
    assert "6시간마다" in ctx.ai.schedule_text("crypto")


def test_stock_research_right_before_open(home):
    ctx = _ctx(home, datetime(2026, 10, 7, 8, 0, tzinfo=KST))  # 수요일, 국내 개장 09:00
    assert ctx.ai.research_slot("kr_stock") is None  # 개장 30분 전(08:30) 전
    ctx.clock.set(datetime(2026, 10, 7, 8, 40, tzinfo=KST))
    assert ctx.ai.research_slot("kr_stock")[0].astimezone(KST).strftime("%H:%M") == "08:30"
    assert ctx.ai.due_research("kr_stock")
    _ran(ctx, "kr_stock")
    assert not ctx.ai.due_research("kr_stock")
    ctx.clock.set(datetime(2026, 10, 7, 11, 15, tzinfo=KST))  # 판단(09:10) + 2시간 지남
    assert ctx.ai.research_slot("kr_stock") is None
    ctx.clock.set(datetime(2026, 10, 10, 8, 40, tzinfo=KST))  # 토요일
    assert ctx.ai.research_slot("kr_stock") is None


def test_us_research_follows_new_york_open_and_dst(home):
    ctx = _ctx(home, datetime(2026, 10, 7, 21, 50, tzinfo=KST))  # 서머타임: 개장 22:30 KST
    assert ctx.ai.research_slot("us_stock") is None
    ctx.clock.set(datetime(2026, 10, 7, 22, 5, tzinfo=KST))
    assert ctx.ai.research_slot("us_stock")[0].astimezone(KST).strftime("%H:%M") == "22:00"
    ctx.clock.set(datetime(2026, 11, 4, 22, 5, tzinfo=KST))  # 서머타임 해제: 개장 23:30 KST
    assert ctx.ai.research_slot("us_stock") is None
    ctx.clock.set(datetime(2026, 11, 4, 23, 5, tzinfo=KST))
    assert ctx.ai.research_slot("us_stock")[0].astimezone(KST).strftime("%H:%M") == "23:00"


def test_scheduled_price_change_applies_by_date(home):
    ctx = _ctx(home, datetime(2026, 12, 31, 12, tzinfo=UTC))
    usage = [LLMUsage("gemini-3.8-flash", 1_000_000, 1_000_000)]
    assert ctx.budget().cost_usd(usage) == (Decimal("4.50"), False)  # 도입가 0.75 + 3.75
    ctx.clock.set(datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc))
    assert ctx.budget().cost_usd(usage) == (Decimal("9.00"), False)  # 표준가 1.50 + 7.50
    assert ctx.budget().estimate_usd("gemini-3.8-flash", 1_000_000, 0, False) == Decimal("1.50")


def test_gemini_thinking_level_sent_for_gemini3_only():
    assert thinking_level_for("gemini-3.8-flash", "max") == "high"
    assert thinking_level_for("gemini-3.8-flash", "medium") == "medium"
    assert thinking_level_for("gemini-2.5-flash", "high") is None  # 2.5 이하는 thinkingBudget 방식

    def handler(req):
        cfg = json.loads(req.content)["generationConfig"]
        assert cfg["thinkingConfig"] == {"thinkingLevel": "high"} and cfg["maxOutputTokens"] == 16000
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}],
                                         "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            p = GeminiProvider(api_key="k", model="gemini-3.8-flash", thinking_level="high", client=c)
            assert (await p.complete_json("s", "u", {}, 16000)).text == "{}"
    asyncio.run(run())


def test_paper_profile_uses_quality_first_ai(home):
    ctx = _ctx(home, datetime(2026, 10, 7, 14, 0, tzinfo=KST))
    a = ctx.settings.ai
    assert (a.provider, a.model, a.effort) == ("gemini", "gemini-3.8-flash", "high")
    assert a.max_output_tokens >= 16000 and a.crypto_research_interval_hours == 6
    assert a.pricing["gemini-3.8-flash"].changes_on == "2027-01-01"
