"""Model fallback chain.

A single LLM call can fail because a provider is rate-limited, overloaded or the
specific model rejects a JSON contract.  Production agents should degrade to another
model instead of failing the task.  ``FallbackLLM`` tries each configured model in
order and only raises after the whole chain fails.

Each provider instance has its own TPM window, so a fallback model also gives the run
a second rate-limit bucket on providers with per-model quotas (e.g. Groq free tier).
"""

from __future__ import annotations

import logging

from app.llm.base import BaseLLM, ChatMessage, LLMError, LLMResponse

logger = logging.getLogger(__name__)


class FallbackLLM(BaseLLM):
    def __init__(self, providers: list[BaseLLM]) -> None:
        if not providers:
            raise ValueError("FallbackLLM requires at least one provider")
        self._providers = providers
        self.provider = providers[0].provider
        self.model = providers[0].model

    @property
    def models(self) -> list[str]:
        return [provider.model for provider in self._providers]

    def invoke(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        errors: list[str] = []
        for index, provider in enumerate(self._providers):
            try:
                return provider.invoke(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except LLMError as exc:
                errors.append(f"{provider.model}: {type(exc).__name__}: {exc}")
                if index < len(self._providers) - 1:
                    logger.warning(
                        "llm_model_fallback",
                        extra={
                            "failed_model": provider.model,
                            "fallback_model": self._providers[index + 1].model,
                            "error": str(exc)[:300],
                        },
                    )
        raise LLMError("all models in the fallback chain failed: " + " | ".join(errors))

    def health(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "configured": True,
            "fallback_models": self.models[1:],
        }


__all__ = ["FallbackLLM"]
