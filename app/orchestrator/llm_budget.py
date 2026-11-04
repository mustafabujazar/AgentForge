"""Per-run LLM accounting: call budget, token aggregation and observability.

The workflow enforces ``settings.llm_max_calls_per_run`` so a runaway loop cannot
burn an API quota.  Callers are expected to *degrade gracefully* when the budget is
exhausted: planner → heuristic plan, evaluator → rules only, agents → deterministic
fallback, synthesizer → template report.  The run still completes.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from app.llm.base import BaseLLM, ChatMessage, LLMError, LLMResponse, TokenUsage

logger = logging.getLogger(__name__)

_RETRY_HINT_RE = re.compile(r"try again in ([\d.]+)\s*(?:s\b|seconds?)", re.IGNORECASE)


def _rate_limit_wait(error_text: str, attempt: int) -> float:
    """Seconds to wait before retrying a provider rate limit."""
    match = _RETRY_HINT_RE.search(error_text)
    if match:
        try:
            return min(30.0, max(1.0, float(match.group(1)) + 0.75))
        except ValueError:  # pragma: no cover - defensive
            pass
    return min(30.0, 2.5 * (2 ** (attempt - 1)))


def _is_rate_limit(error_text: str) -> bool:
    lowered = error_text.lower()
    return "429" in lowered or "rate limit" in lowered or "tpm" in lowered or "tokens per minute" in lowered


class LLMBudgetExceeded(LLMError):
    """Raised when a run has consumed its configured LLM call budget."""


class RunLLM:
    """Budget-enforcing, token-accounting facade over a provider's ``BaseLLM``."""

    def __init__(
        self,
        inner: BaseLLM,
        *,
        max_calls: int = 40,
        on_call: Callable[[dict[str, Any]], None] | None = None,
        initial_usage: TokenUsage | dict[str, Any] | None = None,
        min_interval_seconds: float = 1.0,
        rate_limit_retries: int = 6,
    ) -> None:
        self._inner = inner
        self._max_calls = max(1, int(max_calls))
        self._calls = 0
        self._usage = TokenUsage()
        self._on_call = on_call
        self._lock = threading.Lock()
        self._pace_lock = threading.Lock()
        self._last_call_at = 0.0
        self._min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._rate_limit_retries = max(0, int(rate_limit_retries))
        if initial_usage is not None:
            self._restore_usage(initial_usage)

    # -- properties -------------------------------------------------------
    @property
    def provider(self) -> str:
        return getattr(self._inner, "provider", "unknown")

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "unknown")

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    @property
    def exhausted(self) -> bool:
        return self._calls >= self._max_calls

    @property
    def remaining(self) -> int:
        return max(0, self._max_calls - self._calls)

    def usage_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self._usage.prompt_tokens,
            "completion_tokens": self._usage.completion_tokens,
            "total_tokens": self._usage.total_tokens,
            "calls": self._calls,
            "model": self._usage.model or self.model,
            "provider": self._usage.provider or self.provider,
        }

    # -- invocation -------------------------------------------------------
    def invoke(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        agent: str = "",
    ) -> LLMResponse:
        with self._lock:
            if self._calls >= self._max_calls:
                raise LLMBudgetExceeded(
                    f"LLM call budget exhausted ({self._max_calls} calls per run)"
                )
            self._calls += 1
            call_index = self._calls
        response: LLMResponse | None = None
        attempt = 0
        last_error: Exception | None = None
        while attempt <= self._rate_limit_retries:
            self._pace()
            try:
                response = self._inner.invoke(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except LLMError as exc:
                last_error = exc
                text = str(exc)
                if attempt < self._rate_limit_retries and _is_rate_limit(text):
                    wait = _rate_limit_wait(text, attempt + 1)
                    logger.warning(
                        "llm_rate_limited_retrying",
                        extra={
                            "agent": agent,
                            "attempt": attempt + 1,
                            "wait_seconds": round(wait, 2),
                        },
                    )
                    time.sleep(wait)
                    attempt += 1
                    continue
                self._emit_call(
                    {
                        "agent": agent,
                        "call": call_index,
                        "ok": False,
                        "provider": self.provider,
                        "model": self.model,
                        "latency_ms": 0,
                        "error": text[:400],
                    }
                )
                raise
            except Exception as exc:  # unexpected provider bug → normalise to LLMError
                self._emit_call(
                    {
                        "agent": agent,
                        "call": call_index,
                        "ok": False,
                        "provider": self.provider,
                        "model": self.model,
                        "error": f"{type(exc).__name__}: {exc}"[:400],
                    }
                )
                raise LLMError(f"{type(exc).__name__}: {exc}") from exc
            break

        if response is None:  # pragma: no cover - defensive
            raise LLMError(f"LLM call failed: {last_error}")

        self._accumulate(response)
        self._emit_call(
            {
                "agent": agent,
                "call": call_index,
                "ok": True,
                "provider": response.provider or self.provider,
                "model": response.model or self.model,
                "latency_ms": response.latency_ms,
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            }
        )
        return response

    # -- internals --------------------------------------------------------
    def _pace(self) -> None:
        """Space out logical calls so free-tier TPM limits are less likely to trip."""
        if self._min_interval_seconds <= 0:
            return
        with self._pace_lock:
            now = time.monotonic()
            wait = self._min_interval_seconds - (now - self._last_call_at)
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    def _accumulate(self, response: LLMResponse) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            usage = TokenUsage(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                model=getattr(response, "model", ""),
                provider=getattr(response, "provider", ""),
            )
        with self._lock:
            self._usage.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self._usage.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            self._usage.total_tokens += int(getattr(usage, "total_tokens", 0) or 0)
            if not self._usage.model:
                self._usage.model = getattr(response, "model", "") or self._usage.model
            if not self._usage.provider:
                self._usage.provider = getattr(response, "provider", "") or self._usage.provider

    def _restore_usage(self, usage: TokenUsage | dict[str, Any]) -> None:
        if isinstance(usage, dict):
            self._usage = TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
                model=str(usage.get("model", "") or ""),
                provider=str(usage.get("provider", "") or ""),
            )
            self._calls = int(usage.get("calls", 0) or 0)
        elif isinstance(usage, TokenUsage):
            self._usage = usage

    def _emit_call(self, payload: dict[str, Any]) -> None:
        if self._on_call is None:
            return
        try:
            self._on_call(payload)
        except Exception:  # pragma: no cover - observability must not break the run
            logger.exception("llm_call_observer_failed")


__all__ = ["LLMBudgetExceeded", "RunLLM"]
