import asyncio
import json

import httpx
import pytest

from aifund.ai.gemini import GeminiProvider
from aifund.ai.provider import LLMError
from aifund.core.secrets import load_mode_secrets, redact
from aifund.data.news import NewsCollector
from helpers import make_env


def test_gemini_contract_and_thinking_usage():
    def handler(req):
        body = json.loads(req.content)
        assert req.headers["x-goog-api-key"] == "test-key"
        assert "key=" not in str(req.url)
        assert "tools" not in body
        assert body["generationConfig"]["responseJsonSchema"] == {"type": "object"}
        assert body["generationConfig"]["maxOutputTokens"] == 256
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": "private thought", "thought": True}, {"text": "{}"}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 12,
                              "thoughtsTokenCount": 30, "cachedContentTokenCount": 50},
            "modelVersion": "gemini-version",
        })
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = GeminiProvider(api_key="test-key", model="gemini-3.5-flash-lite", client=client)
            r = await p.complete_json("sys", "user", {"type": "object"}, 256)
            assert r.text == "{}" and r.usages[0].output_tokens == 42
            assert r.usages[0].input_tokens == 100 and r.usages[0].cache_read_tokens == 0
    asyncio.run(run())


@pytest.mark.parametrize("status,kind", [(400, "bad_request"), (403, "auth"), (429, "rate_limited"), (500, "server"), (200, "server")])
def test_gemini_errors_hide_response_and_missing_usage(status, kind):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"secret": "hidden-value"}))) as c:
            p = GeminiProvider(api_key="test", model="test", client=c)
            with pytest.raises(LLMError) as err:
                await p.complete_json("", "", {}, 256)
            assert err.value.kind == kind and "hidden-value" not in str(err.value)
    asyncio.run(run())


@pytest.mark.parametrize("finish,refusal,stop", [("MAX_TOKENS", False, "max_tokens"), ("SAFETY", True, "SAFETY")])
def test_gemini_finish_reasons(finish, refusal, stop):
    async def run():
        payload = {"candidates": [{"finishReason": finish}], "usageMetadata": {"promptTokenCount": 10}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as c:
            result = await GeminiProvider(api_key="test", model="test", client=c).complete_json("", "", {}, 256)
            assert result.refusal == refusal and result.stop_reason == stop
    asyncio.run(run())


def test_naver_dedup_dates_and_failure(tmp_path):
    env = make_env(tmp_path)
    calls = []
    def handler(req):
        calls.append(req)
        assert req.headers["X-Naver-Client-Secret"] == "secret"
        if len(calls) == 3:
            return httpx.Response(403, json={"errorMessage": "secret"})
        return httpx.Response(200, json={"items": [{
            "title": "<b>비트코인</b> &amp; 시장", "description": "<b>뉴스</b>",
            "originallink": "https://example.com/news/1", "pubDate": "Tue, 29 Sep 2026 09:00:00 +0900",
        }, {"title": "bad link", "link": "javascript:alert(1)", "pubDate": "invalid"}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            news = NewsCollector(env.db, env.clock, client)
            assert (await news.fetch_naver("id", "secret", "비트코인", "crypto")).new_items == 1
            assert (await news.fetch_naver("id", "secret", "코인", "crypto")).new_items == 0
            result = await news.fetch_naver("id", "secret", "코인", "crypto")
            assert not result.ok and result.error == "HTTP 403"  # 상태 코드만, 응답 본문·키 없음
            row = env.db.query_one("SELECT * FROM sources WHERE source_id LIKE 'naver:%'")
            assert row["title"] == "비트코인 & 시장" and row["published_at"].startswith("2026-09-29T00:00")
    asyncio.run(run())


def test_keys_isolated_and_redacted(monkeypatch):
    for name in ("GEMINI_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"):
        monkeypatch.setenv(name, "private-value-" + name)
    demo = load_mode_secrets("offline_demo")
    assert demo.gemini_api_key is None and demo.naver_client_secret is None
    paper = load_mode_secrets("internal_paper")
    assert paper.gemini_api_key and paper.naver_client_secret and paper.upbit is None
    assert "private-value" not in redact(paper.gemini_api_key + paper.naver_client_secret)


def test_provider_model_mismatch_blocks_calls(home):
    from aifund.core.paths import mode_paths
    from aifund.service.context import build_context

    ctx = build_context(mode_paths("internal_paper", home).ensure())
    ctx.settings.ai.provider = "gemini"  # 화면에서 공급자만 바꾸고 모델은 그대로 둔 경우
    ok, why = ctx.ai.availability()
    assert not ok and "불일치" in why
    ctx.settings.ai.model = "gemini-3.5-flash-lite"
    ok, why = ctx.ai.availability()
    assert not ok and "불일치" not in why  # 이제는 키가 없다는 이유만 남는다


def test_research_focus_sent_only_when_set(home):
    from datetime import datetime
    from aifund.core.paths import mode_paths
    from aifund.core.timeutil import UTC, ManualClock
    from aifund.service.context import build_context
    from aifund.service.cycle import DecisionCycle

    ctx = build_context(mode_paths("offline_demo", home).ensure(), clock=ManualClock(datetime(2026, 9, 28, 1, tzinfo=UTC)))
    cycle = DecisionCycle(ctx)
    asyncio.run(ctx.markets["crypto"].collector.refresh_instruments("crypto", ctx.settings.markets["crypto"].instruments))
    asyncio.run(cycle.run("crypto", trigger="manual"))
    snap = cycle.last_snapshots["crypto"]
    provider = ctx.provider()
    asyncio.run(ctx.ai.research("crypto", snap, cycle.signals_payload(snap), "manual"))
    assert "대표의 연구 관심사" not in provider.calls[-1][1]
    ctx.settings.ai.research_focus = "반도체 우선"
    asyncio.run(ctx.ai.research("crypto", snap, cycle.signals_payload(snap), "manual"))
    assert "대표의 연구 관심사: 반도체 우선" in provider.calls[-1][1]


@pytest.mark.parametrize("missing_usage", [False, True])
def test_gemini_service_budget_and_schema(home, missing_usage):
    from pydantic import BaseModel
    from aifund.core.paths import mode_paths
    from aifund.service.context import build_context
    from aifund.config.settings import AISettings

    class Reply(BaseModel):
        summary: str

    ctx = build_context(mode_paths("internal_paper", home).ensure())
    ctx.settings.ai.provider = "gemini"
    ctx.settings.ai.model = "gemini-3.5-flash-lite"
    ctx.settings.ai.pricing = AISettings().pricing
    def handler(req):
        assert json.loads(req.content)["generationConfig"]["responseJsonSchema"] == Reply.model_json_schema()
        data = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"summary":"ok"}'}]}}]}
        if not missing_usage:
            data["usageMetadata"] = {"promptTokenCount": 100, "candidatesTokenCount": 10, "thoughtsTokenCount": 20}
        return httpx.Response(200, json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            ctx._provider = GeminiProvider(api_key="test", model=ctx.settings.ai.model, client=c)
            outcome = await ctx.ai._call(role="research", market="crypto", snapshot_id=None,
                                         system="sys", user="user", model_cls=Reply, trigger="test")
            assert outcome.status == ("error" if missing_usage else "ok")
            row = ctx.db.query_one("SELECT * FROM ai_budget WHERE run_id=?", (outcome.run_id,))
            assert row["status"] == "settled" and outcome.cost_krw > 0
            if missing_usage:
                assert row["actual_krw"] == row["reserved_krw"]
    asyncio.run(run())
