"""LLM provider factory selected from settings."""

from __future__ import annotations

import threading

from app.core.config import Settings, get_settings
from app.core.errors import LLMConfigurationError
from app.llm.base import BaseLLM, LLMError
from app.llm.fallback import FallbackLLM
from app.llm.mock import MockLLM
from app.llm.openai_compat import OpenAICompatibleLLM

_lock = threading.Lock()
_cache: dict[str, BaseLLM] = {}


def _build_groq(settings: Settings, model: str) -> OpenAICompatibleLLM:
    if not settings.groq_api_key:
        raise LLMConfigurationError(
            "GROQ_API_KEY is not configured; set it in .env or use LLM_PROVIDER=mock"
        )
    return OpenAICompatibleLLM(
        provider="groq",
        model=model,
        base_url=settings.groq_base_url,
        api_key=settings.groq_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
        tpm_limit=settings.llm_tpm_limit,
        rate_limit_safety=settings.llm_rate_limit_safety,
        reasoning_effort=settings.llm_reasoning_effort,
    )


def _build_llm(settings: Settings, provider: str | None = None, model: str | None = None) -> BaseLLM:
    provider_name = (provider or settings.llm_provider or "mock").strip().lower()

    if provider_name == "groq":
        primary_model = model or settings.llm_model
        chain: list[BaseLLM] = [_build_groq(settings, primary_model)]
        if not model:
            for fallback_model in settings.llm_fallback_models:
                if fallback_model and fallback_model != primary_model:
                    chain.append(_build_groq(settings, fallback_model))
        if len(chain) == 1:
            return chain[0]
        return FallbackLLM(chain)

    if provider_name == "openai":
        if not settings.openai_api_key:
            raise LLMConfigurationError(
                "OPENAI_API_KEY is not configured; set it in .env or use LLM_PROVIDER=mock"
            )
        return OpenAICompatibleLLM(
            provider="openai",
            model=model or settings.openai_model,
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            timeout_seconds=settings.llm_timeout_seconds,
            tpm_limit=settings.llm_tpm_limit,
            rate_limit_safety=settings.llm_rate_limit_safety,
        )

    if provider_name in {"ollama", "local"}:
        return OpenAICompatibleLLM(
            provider="ollama",
            model=model or settings.ollama_model,
            base_url=settings.ollama_base_url,
            api_key="ollama",
            timeout_seconds=settings.llm_timeout_seconds,
            json_mode_style="format",
            tpm_limit=settings.llm_tpm_limit,
            rate_limit_safety=settings.llm_rate_limit_safety,
        )

    if provider_name == "mock":
        return MockLLM()

    raise LLMConfigurationError(f"unsupported LLM provider: {provider_name!r}")


def get_llm(
    provider: str | None = None,
    model: str | None = None,
    *,
    force: bool = False,
) -> BaseLLM:
    """Return a cached LLM provider instance.

    The cache key includes provider and model so tests can request mocks or
    alternate models without disturbing the default.
    """
    settings = get_settings()
    provider_name = (provider or settings.llm_provider or "mock").strip().lower()
    model_name = model or (
        settings.llm_model
        if provider_name in {"groq", "openai", "ollama"}
        else "mock-1"
    )
    fallback_key = ",".join(settings.llm_fallback_models) if provider_name == "groq" and not model else ""
    key = f"{provider_name}:{model_name}:{fallback_key}"
    if force:
        with _lock:
            _cache.pop(key, None)
    with _lock:
        instance = _cache.get(key)
        if instance is None:
            instance = _build_llm(settings, provider_name, model)
            _cache[key] = instance
    return instance


def reset_llm_cache() -> None:
    with _lock:
        _cache.clear()


def provider_info(settings: Settings | None = None) -> dict[str, object]:
    """Non-throwing description of the configured provider for /health."""
    settings = settings or get_settings()
    provider = (settings.llm_provider or "mock").lower()
    model = settings.llm_model
    configured = True
    if provider == "groq":
        configured = bool(settings.groq_api_key)
        base_url = settings.groq_base_url
    elif provider == "openai":
        configured = bool(settings.openai_api_key)
        base_url = settings.openai_base_url
        model = settings.openai_model
    elif provider in {"ollama", "local"}:
        base_url = settings.ollama_base_url
        model = settings.ollama_model
    else:
        base_url = ""
    return {
        "provider": provider,
        "model": model,
        "configured": configured,
        "base_url": base_url,
        "fallback_models": settings.llm_fallback_models if provider == "groq" else [],
        "tpm_limit": settings.llm_tpm_limit,
    }


__all__ = [
    "BaseLLM",
    "LLMError",
    "MockLLM",
    "OpenAICompatibleLLM",
    "get_llm",
    "provider_info",
    "reset_llm_cache",
]
