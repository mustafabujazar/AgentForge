"""LLM provider abstraction: messages, responses, errors and JSON extraction.

Concrete providers live in sibling modules.  Agents depend only on this interface,
which makes hosted models, local Ollama models and deterministic mocks interchangeable.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import LLMConfigurationError


class LLMError(RuntimeError):
    """Base error for provider/network/response failures."""


class LLMTimeoutError(LLMError):
    """The provider did not respond within the configured timeout."""


class LLMResponseError(LLMError):
    """The provider returned an unusable response."""


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    provider: str = ""

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            model=other.model or self.model,
            provider=other.provider or self.provider,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "provider": self.provider,
        }


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str
    provider: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None


class BaseLLM(ABC):
    provider: str = "base"
    model: str = "unknown"

    @abstractmethod
    def invoke(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Run one chat completion."""

    def health(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "configured": True}

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<{type(self).__name__} provider={self.provider} model={self.model}>"


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from an LLM response.

    Handles markdown fences, preamble text, trailing commas and the occasional
    single-quoted payload emitted by smaller models.
    """
    if not text or not text.strip():
        raise LLMResponseError("empty LLM response")

    candidate = text.strip()

    fenced = _FENCE_RE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    start = candidate.find("{")
    if start == -1:
        raise LLMResponseError("no JSON object found in response")

    # Walk the string to find the matching closing brace (ignoring braces in strings).
    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(candidate)):
        char = candidate[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break

    if end == -1:
        raise LLMResponseError("unbalanced JSON object in response")

    raw = candidate[start:end]
    for attempt in (raw, _strip_trailing_commas(raw), _strip_trailing_commas(raw).replace("'", '"')):
        try:
            parsed = json.loads(attempt)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    raise LLMResponseError("could not parse JSON from response")


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


__all__ = [
    "BaseLLM",
    "ChatMessage",
    "LLMConfigurationError",
    "LLMError",
    "LLMResponse",
    "LLMResponseError",
    "LLMTimeoutError",
    "TokenUsage",
    "extract_json",
]
