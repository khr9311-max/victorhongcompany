"""Gemini REST 어댑터(generateContent). 도구·자동 재시도·모델 자동 대체 없음.

구조화 출력은 responseJsonSchema로 요청하고, 추론(thought) 토큰도 출력 토큰으로 정산한다.
키는 x-goog-api-key 헤더로만 보내며(URL에 넣지 않음) 오류 메시지에 응답 본문을 남기지 않는다.
"""

from typing import Any
from urllib.parse import quote

import httpx

from aifund.ai.provider import LLMError, LLMProvider, LLMResult, LLMUsage


# 설정 ai.effort → Gemini 3 thinkingLevel(low·medium·high). 3.8 Flash는 minimal 미지원, 기본값 medium.
THINKING_LEVELS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def thinking_level_for(model: str, effort: str) -> str | None:
    """Gemini 3 계열만 thinkingLevel을 받는다(2.5 이하는 thinkingBudget 방식이라 보내지 않음)."""
    return THINKING_LEVELS.get(effort) if model.startswith("gemini-3") else None


class GeminiProvider(LLMProvider):
    name = "gemini"
    is_paid = True  # 무료 티어 키도 같은 보수적 예산 예약·정산을 거친다

    def __init__(self, *, api_key: str, model: str, timeout: float = 120, thinking_level: str | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        super().__init__(model)
        self._key = api_key
        self._thinking_level = thinking_level
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int,
                            effort: str | None = None) -> LLMResult:
        config: dict[str, Any] = {
            "maxOutputTokens": max_tokens,  # 추론 토큰 포함 상한
            "responseMimeType": "application/json", "responseJsonSchema": schema,
        }
        level = thinking_level_for(self.model, effort) if effort else self._thinking_level
        if level:
            config["thinkingConfig"] = {"thinkingLevel": level}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
        try:
            response = await self._client.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + quote(self.model, safe="") + ":generateContent",
                headers={"x-goog-api-key": self._key}, json=body,
            )
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", "Gemini request timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMError("network", "Gemini connection failed") from exc
        if response.status_code != 200:
            code = response.status_code
            kind = {400: "bad_request", 401: "auth", 403: "auth", 404: "bad_request", 429: "rate_limited"}.get(code, "server")
            raise LLMError(kind, f"Gemini HTTP {code}")
        try:
            data = response.json()
            usage = data.get("usageMetadata")
            if not isinstance(usage, dict) or "promptTokenCount" not in usage:
                raise ValueError("Missing usage")
            inp = int(usage["promptTokenCount"])
            out = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
            if inp < 0 or out < 0:
                raise ValueError("Negative usage")
            candidates = data.get("candidates") or []
            candidate = candidates[0] if candidates else {}
            reason = candidate.get("finishReason")
            refusal = bool((data.get("promptFeedback") or {}).get("blockReason")) or reason in {
                "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
            parts = (candidate.get("content") or {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            return LLMResult(
                text=None if refusal else text,
                usages=[LLMUsage(self.model, inp, out)],
                stop_reason="max_tokens" if reason == "MAX_TOKENS" else reason,
                request_id=data.get("responseId"), served_model=data.get("modelVersion") or self.model,
                refusal=refusal,
            )
        except (ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
            # 사용량을 모르면 0원으로 정산하지 않고 예약액으로 보수 정산한다(서비스의 LLMError 처리)
            raise LLMError("server", "Invalid Gemini response or missing usage") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
