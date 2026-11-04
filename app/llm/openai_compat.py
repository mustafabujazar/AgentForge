"""OpenAI-compatible chat completion provider.

Works with Groq, OpenAI, Ollama (``/v1`` compatible endpoint), vLLM and most
OpenAI-compatible gateways.  Uses httpx directly to avoid coupling to a specific
LangChain integration version.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from typing import Any

import httpx

from app.llm.base import (
    BaseLLM,
    ChatMessage,
    LLMError,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    TokenUsage,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class TokenRateLimiter:
    """Rolling 60-second token window to stay under a provider TPM limit.

    Providers count prompt + completion tokens per minute.  We record actual usage
    after each response and wait before a call whose estimated cost would exceed the
    window.  This turns a hard HTTP 429 into a bounded, predictable pause, which is
    what a production agent should do under a free/low tier.
    """

    def __init__(self, tpm_limit: int = 0, safety: float = 0.9) -> None:
        self.limit = max(0, int(tpm_limit))
        self.safety = min(1.0, max(0.1, float(safety)))
        self._window: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def wait_for_capacity(self, estimated_tokens: int) -> None:
        if self.limit <= 0:
            return
        budget = max(1, int(self.limit * self.safety))
        estimated = max(1, min(int(estimated_tokens), budget))
        while True:
            now = time.monotonic()
            with self._lock:
                while self._window and now - self._window[0][0] > 60.0:
                    self._window.popleft()
                used = sum(tokens for _, tokens in self._window)
                if used + estimated <= budget or not self._window:
                    return
                wait = max(0.5, 60.0 - (now - self._window[0][0]) + 0.25)
            logger.info(
                "llm_rate_limit_wait",
                extra={"wait_seconds": round(wait, 1), "window_tokens": used, "budget": budget},
            )
            time.sleep(min(wait, 30.0))

    def record(self, tokens: int) -> None:
        if self.limit <= 0 or tokens <= 0:
            return
        with self._lock:
            self._window.append((time.monotonic(), int(tokens)))

    def estimate(self, messages: list["ChatMessage"], max_tokens: int | None) -> int:
        chars = sum(len(message.content) for message in messages)
        prompt_estimate = int(chars / 3.5) + 16
        # JSON replies are small; cap the completion estimate instead of assuming
        # the full max_tokens budget (which would over-throttle the run).
        completion_estimate = min(int(max_tokens or 900), 900)
        return prompt_estimate + completion_estimate


class OpenAICompatibleLLM(BaseLLM):
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 90,
        max_retries: int = 3,
        json_mode_style: str = "response_format",
        tpm_limit: int = 0,
        rate_limit_safety: float = 0.9,
        reasoning_effort: str | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.json_mode_style = json_mode_style
        self.reasoning_effort = reasoning_effort or None
        self._rate_limiter = TokenRateLimiter(tpm_limit, rate_limit_safety)

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(
        self,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        json_mode: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.to_dict() for message in messages],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            if self.json_mode_style == "format":
                payload["format"] = "json"
            else:
                payload["response_format"] = {"type": "json_object"}
        if self.reasoning_effort and "gpt-oss" in (self.model or "").lower():
            payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def invoke(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        if not messages:
            raise LLMError("no messages supplied")
        temperature = 0.2 if temperature is None else temperature
        payload = self._payload(messages, temperature, max_tokens, json_mode)
        url = f"{self.base_url}/chat/completions"
        response: httpx.Response | None = None
        last_error: Exception | None = None

        self._rate_limiter.wait_for_capacity(self._rate_limiter.estimate(messages, max_tokens))
        started = time.perf_counter()

        with httpx.Client(timeout=httpx.Timeout(self.timeout_seconds, connect=15.0)) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = client.post(url, headers=self._headers(), json=payload)
                except httpx.TimeoutException as exc:
                    last_error = exc
                    if attempt < self.max_retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise LLMTimeoutError(
                        f"{self.provider} timed out after {self.timeout_seconds}s"
                    ) from exc
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt < self.max_retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise LLMError(f"{self.provider} request failed: {exc}") from exc

                if response.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                    if response.status_code == 429:
                        retry_after = _retry_after_seconds(response)
                        if attempt == 0:
                            wait = min(retry_after or 3.0, 8.0)
                            logger.warning(
                                "llm_rate_limited_retrying",
                                extra={"wait_seconds": round(wait, 1), "model": self.model},
                            )
                            time.sleep(wait)
                            continue
                        # Second 429: stop retrying so the fallback chain can switch models.
                        break
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break

        assert response is not None  # for type checkers
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            detail = _error_detail(response)
            raise LLMResponseError(
                f"{self.provider} returned HTTP {response.status_code}: {detail}"
            )

        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMResponseError(f"{self.provider} returned non-JSON response") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMResponseError(f"{self.provider} returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not content and message.get("reasoning"):
            content = message["reasoning"]
        if not content:
            raise LLMResponseError(f"{self.provider} returned an empty message")

        usage_raw = data.get("usage") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
            model=data.get("model") or self.model,
            provider=self.provider,
        )
        self._rate_limiter.record(usage.prompt_tokens + usage.completion_tokens)
        finish_reason = choices[0].get("finish_reason")

        logger.info(
            "llm_call_completed",
            extra={
                "provider": self.provider,
                "model": self.model,
                "latency_ms": latency_ms,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "json_mode": json_mode,
            },
        )
        return LLMResponse(
            content=content,
            model=usage.model,
            provider=self.provider,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            raw=data,
        )


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:500]
            if isinstance(error, str):
                return error[:500]
    except Exception:
        pass
    return response.text[:500]


def _retry_after_seconds(response: httpx.Response) -> float:
    """Parse Retry-After header or the usual Groq 'try again in 13.7s' message."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, float(header))
        except (TypeError, ValueError):
            pass
    try:
        body = response.json()
        message = ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
            elif isinstance(error, str):
                message = error
            if not message:
                message = str(body.get("message") or "")
    except Exception:
        message = response.text[:1000]
    match = re.search(r"try again in\s+([\d.]+)\s*s", message or "", re.IGNORECASE)
    if match:
        return max(0.0, float(match.group(1)) + 0.5)
    return 0.0
