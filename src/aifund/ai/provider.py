"""LLMProvider 인터페이스와 구현.

- AnthropicProvider: 공식 anthropic SDK(AsyncAnthropic). 구조화 출력(output_config.format=json_schema),
  사전 토큰 계산(messages.count_tokens), 거절 시 서버측 폴백(fallbacks="default").
  사용량은 usage.iterations(모델별)로 정산한다.
- GeminiProvider(ai/gemini.py): Google Gemini REST generateContent. 사전 토큰 계산 없이 보수 추정으로 예약한다.
- OllamaProvider: 로컬 모델(선택). 비용 0. 이 개발 환경에서는 미검증.
- DemoProvider: offline_demo/테스트용 결정적 가짜 응답(명시적으로 [데모] 표시).
AI 제공자에는 주문·쉘·설정 변경 권한이 없다(도구 미제공, JSON 텍스트만 반환).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class LLMUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class LLMResult:
    text: str | None
    usages: list[LLMUsage]
    stop_reason: str | None
    request_id: str | None = None
    served_model: str | None = None
    refusal: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"[{kind}] {message}")
        self.kind = kind  # auth / rate_limited / timeout / server / bad_request / network


class LLMProvider(ABC):
    name = "base"
    is_paid = False

    def __init__(self, model: str) -> None:
        self.model = model

    async def count_tokens(self, system: str, user: str, schema: dict[str, Any]) -> int | None:
        return None

    @abstractmethod
    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult: ...

    async def close(self) -> None:
        return None


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    is_paid = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        effort: str = "medium",
        use_fallbacks: bool = True,
        timeout: float = 120.0,
        max_retries: int = 1,
        http_client: Any = None,
    ) -> None:
        super().__init__(model)
        from anthropic import AsyncAnthropic

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = AsyncAnthropic(**kwargs)
        self.effort = effort
        self.use_fallbacks = use_fallbacks

    @staticmethod
    def _wrap(exc: Exception) -> LLMError:
        import anthropic

        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return LLMError("auth", str(exc)[:300])
        if isinstance(exc, anthropic.RateLimitError):
            return LLMError("rate_limited", str(exc)[:300])
        if isinstance(exc, anthropic.APITimeoutError):
            return LLMError("timeout", str(exc)[:300])
        if isinstance(exc, anthropic.BadRequestError | anthropic.NotFoundError | anthropic.UnprocessableEntityError):
            return LLMError("bad_request", str(exc)[:300])
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMError("network", str(exc)[:300])
        if isinstance(exc, anthropic.APIStatusError):
            return LLMError("server", str(exc)[:300])
        return LLMError("unknown", repr(exc)[:300])

    async def count_tokens(self, system: str, user: str, schema: dict[str, Any]) -> int | None:
        try:
            r = await self.client.messages.count_tokens(
                model=self.model,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            return int(r.input_tokens)
        except Exception as exc:  # 토큰 계산 실패 시 보수적 추정으로 대체
            log.warning("토큰 계산 실패(보수적 추정 사용): %s", self._wrap(exc))
            return None

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult:
        req: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}, "effort": self.effort},
        }
        try:
            if self.use_fallbacks:
                resp = await self.client.beta.messages.create(**req, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                resp = await self.client.messages.create(**req)
        except Exception as exc:
            raise self._wrap(exc) from exc
        usages: list[LLMUsage] = []
        iterations = getattr(resp.usage, "iterations", None) or []
        for it in iterations:
            model = getattr(it, "model", None) or resp.model
            usages.append(
                LLMUsage(
                    model=model,
                    input_tokens=int(getattr(it, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(it, "output_tokens", 0) or 0),
                    cache_read_tokens=int(getattr(it, "cache_read_input_tokens", 0) or 0),
                    cache_write_tokens=int(getattr(it, "cache_creation_input_tokens", 0) or 0),
                )
            )
        if not usages:
            u = resp.usage
            usages.append(
                LLMUsage(
                    model=resp.model,
                    input_tokens=int(u.input_tokens or 0),
                    output_tokens=int(u.output_tokens or 0),
                    cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
                    cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
                )
            )
        refusal = resp.stop_reason == "refusal"
        text = None
        if not refusal:
            text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        return LLMResult(
            text=text,
            usages=usages,
            stop_reason=resp.stop_reason,
            request_id=getattr(resp, "_request_id", None),
            served_model=resp.model,
            refusal=refusal,
        )

    async def close(self) -> None:
        await self.client.close()


class OllamaProvider(LLMProvider):
    """로컬 Ollama(선택 기능). /api/chat 의 format 필드에 JSON 스키마를 전달한다."""

    name = "ollama"
    is_paid = False

    def __init__(self, *, model: str, host: str = "http://127.0.0.1:11434", timeout: float = 300.0) -> None:
        super().__init__(model)
        self.host = host.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "options": {"num_predict": max_tokens, "temperature": 0},
        }
        try:
            r = await self._client.post(self.host + "/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMError("network", exc.__class__.__name__) from exc
        if r.status_code != 200:
            raise LLMError("server", f"HTTP {r.status_code}")
        d = r.json()
        return LLMResult(
            text=(d.get("message") or {}).get("content"),
            usages=[LLMUsage(self.model, int(d.get("prompt_eval_count") or 0), int(d.get("eval_count") or 0))],
            stop_reason=d.get("done_reason"),
            served_model=self.model,
        )

    async def close(self) -> None:
        await self._client.aclose()


class DemoProvider(LLMProvider):
    """결정적 가짜 응답. responder(system, user) -> dict. 비용 0, 결과에 [데모]가 표시된다."""

    name = "demo"
    is_paid = False

    def __init__(self, responder: Callable[[str, str], dict[str, Any]], model: str = "demo-model") -> None:
        super().__init__(model)
        self.responder = responder
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult:
        self.calls.append((system, user))
        data = self.responder(system, user)
        text = json.dumps(data, ensure_ascii=False)
        return LLMResult(text=text, usages=[LLMUsage(self.model, len(user) // 3, len(text) // 3)], stop_reason="end_turn",
                         served_model=self.model)
