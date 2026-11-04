"""Deterministic mock LLM provider.

Used for offline development, tests and as a safe fallback when no API key is
configured.  Tests can inject scripted responses or a callable responder.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence

from app.llm.base import BaseLLM, ChatMessage, LLMResponse, TokenUsage

Responder = Callable[[list[ChatMessage], bool], str]


class MockLLM(BaseLLM):
    provider = "mock"
    model = "mock-1"

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        responder: Responder | None = None,
        default_json: dict | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._responder = responder
        self._default_json = default_json or {
            "task_id": "task-1",
            "summary": "Mock agent completed the task.",
            "findings": [],
            "artifacts": [],
            "confidence": 0.6,
        }
        self.calls: list[dict] = []

    def invoke(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        started = time.perf_counter()
        self.calls.append(
            {
                "messages": [m.to_dict() for m in messages],
                "json_mode": json_mode,
                "temperature": temperature,
            }
        )
        if self._responder is not None:
            content = self._responder(messages, json_mode)
        elif self._responses:
            content = self._responses.pop(0)
        elif json_mode:
            content = json.dumps(self._default_json)
        else:
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            content = f"[mock-llm] acknowledged: {last_user[:200]}"
        latency_ms = int((time.perf_counter() - started) * 1000)
        return LLMResponse(
            content=content,
            model=self.model,
            provider=self.provider,
            usage=TokenUsage(
                prompt_tokens=sum(len(m.content.split()) for m in messages),
                completion_tokens=len(content.split()),
                total_tokens=sum(len(m.content.split()) for m in messages) + len(content.split()),
                model=self.model,
                provider=self.provider,
            ),
            latency_ms=latency_ms,
            finish_reason="stop",
        )

    def queue(self, *responses: str) -> None:
        """Append scripted responses (used by tests)."""
        self._responses.extend(responses)
