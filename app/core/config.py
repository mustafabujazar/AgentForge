"""AgentForge application configuration.

All runtime configuration is environment-driven (see `.env.example`).  This module
deliberately uses plain dataclasses + python-dotenv instead of pydantic-settings so
the platform runs on the pinned dependency set with no extra installs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Load .env once, before any settings object is built.  Real environment variables
# win over .env values so containers/platform env always take precedence.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value is not None else default


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on", "y"}


def _list(name: str, default: list[str]) -> list[str]:
    raw = _str(name)
    if not raw:
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _default_llm_provider() -> str:
    explicit = _str("LLM_PROVIDER")
    if explicit:
        return explicit.lower()
    if _str("GROQ_API_KEY"):
        return "groq"
    if _str("OPENAI_API_KEY"):
        return "openai"
    if _str("OLLAMA_BASE_URL"):
        return "ollama"
    return "mock"


def _normalize_database_url(url: str) -> str:
    """Normalize SQLAlchemy URLs and make Postgres use the psycopg v3 driver."""
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _default_database_url() -> str:
    data_dir = PROJECT_ROOT / "data"
    return f"sqlite:///{(data_dir / 'agentforge.db').as_posix()}"


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for AgentForge."""

    app_name: str = "AgentForge"
    app_version: str = "1.0.0"
    environment: str = "development"
    debug: bool = False

    # --- security / auth -------------------------------------------------
    secret_key: str = "change-me-in-production-agentforge"
    access_token_expire_minutes: int = 720
    demo_user_email: str = "demo@agentforge.io"
    demo_user_password: str = "demo1234"
    demo_user_name: str = "Demo Operator"

    # --- persistence -----------------------------------------------------
    database_url: str = ""
    sqlite_echo: bool = False

    # --- LLM -------------------------------------------------------------
    llm_provider: str = "groq"
    llm_model: str = "openai/gpt-oss-120b"
    llm_fallback_models: list[str] = field(default_factory=list)
    llm_temperature: float = 0.2
    llm_max_tokens: int = 3000
    llm_timeout_seconds: int = 90
    llm_max_calls_per_run: int = 40
    # Rolling tokens-per-minute budget for the provider (0 = no pacing). Free Groq
    # accounts are limited to 8k TPM, so the demo paces calls to avoid HTTP 429s.
    llm_tpm_limit: int = 0
    llm_rate_limit_safety: float = 0.9
    # Groq gpt-oss models accept reasoning_effort=low|medium|high; low cuts hidden
    # reasoning tokens substantially and makes JSON contracts more predictable.
    llm_reasoning_effort: str = ""
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "llama3.1"

    # --- orchestration ---------------------------------------------------
    max_agent_retries: int = 2
    eval_pass_score: float = 0.72
    step_timeout_seconds: int = 240
    hitl_enabled: bool = True
    auto_approve_default: bool = False
    planner_temperature: float = 0.1

    # --- RAG -------------------------------------------------------------
    embedding_dim: int = 384
    rag_top_k: int = 5
    rag_min_score: float = 0.08
    rag_chunk_size: int = 700
    rag_chunk_overlap: int = 100

    # --- demo data -------------------------------------------------------
    sales_db_path: str = ""
    knowledge_dir: str = ""
    artifacts_dir: str = ""
    frontend_dist_dir: str = ""

    # --- API -------------------------------------------------------------
    cors_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )

    # --- observability ---------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""

    # -- derived helpers --------------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sales_db(self) -> Path:
        return Path(self.sales_db_path or PROJECT_ROOT / "data" / "demo" / "sales.db")

    @property
    def knowledge_path(self) -> Path:
        return Path(self.knowledge_dir or PROJECT_ROOT / "data" / "knowledge")

    @property
    def artifacts_path(self) -> Path:
        return Path(self.artifacts_dir or PROJECT_ROOT / "artifacts")

    @property
    def frontend_dist(self) -> Path:
        return Path(self.frontend_dist_dir or PROJECT_ROOT / "frontend" / "dist")

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = _normalize_database_url(_str("DATABASE_URL")) or _default_database_url()
        if "YOUR_PASSWORD" in database_url or "CHANGE_ME" in database_url:
            # .env.example placeholder committed by mistake: fall back to local SQLite
            # instead of hanging on an unreachable database.
            import logging

            logging.getLogger("agentforge.config").warning(
                "DATABASE_URL contains a placeholder password; falling back to SQLite "
                "for local development. Set a real DATABASE_URL for PostgreSQL."
            )
            database_url = _default_database_url()
        defaults = cls()
        return cls(
            app_name=_str("APP_NAME", defaults.app_name),
            app_version=_str("APP_VERSION", defaults.app_version),
            environment=_str("APP_ENV", _str("ENVIRONMENT", defaults.environment)),
            debug=_bool("DEBUG", defaults.debug),
            secret_key=_str("SECRET_KEY", defaults.secret_key),
            access_token_expire_minutes=_int(
                "ACCESS_TOKEN_EXPIRE_MINUTES", defaults.access_token_expire_minutes
            ),
            demo_user_email=_str("DEMO_USER_EMAIL", defaults.demo_user_email).lower(),
            demo_user_password=_str("DEMO_USER_PASSWORD", defaults.demo_user_password),
            demo_user_name=_str("DEMO_USER_NAME", defaults.demo_user_name),
            database_url=database_url,
            sqlite_echo=_bool("SQLITE_ECHO", defaults.sqlite_echo),
            llm_provider=_default_llm_provider(),
            llm_model=_str("LLM_MODEL") or _str("GROQ_MODEL", defaults.llm_model),
            llm_fallback_models=_list("LLM_FALLBACK_MODELS", []),
            llm_temperature=_float("LLM_TEMPERATURE", defaults.llm_temperature),
            llm_max_tokens=_int("LLM_MAX_TOKENS", defaults.llm_max_tokens),
            llm_timeout_seconds=_int("LLM_TIMEOUT_SECONDS", defaults.llm_timeout_seconds),
            llm_max_calls_per_run=_int("LLM_MAX_CALLS_PER_RUN", defaults.llm_max_calls_per_run),
            llm_tpm_limit=_int("LLM_TPM_LIMIT", defaults.llm_tpm_limit),
            llm_rate_limit_safety=_float("LLM_RATE_LIMIT_SAFETY", defaults.llm_rate_limit_safety),
            llm_reasoning_effort=_str("LLM_REASONING_EFFORT", defaults.llm_reasoning_effort),
            groq_api_key=_str("GROQ_API_KEY"),
            groq_base_url=_str("GROQ_BASE_URL", defaults.groq_base_url),
            openai_api_key=_str("OPENAI_API_KEY"),
            openai_base_url=_str("OPENAI_BASE_URL", defaults.openai_base_url),
            openai_model=_str("OPENAI_MODEL", defaults.openai_model),
            ollama_base_url=_str("OLLAMA_BASE_URL", defaults.ollama_base_url),
            ollama_model=_str("OLLAMA_MODEL", defaults.ollama_model),
            max_agent_retries=_int("MAX_AGENT_RETRIES", defaults.max_agent_retries),
            eval_pass_score=_float("EVAL_PASS_SCORE", defaults.eval_pass_score),
            step_timeout_seconds=_int("STEP_TIMEOUT_SECONDS", defaults.step_timeout_seconds),
            hitl_enabled=_bool("HITL_ENABLED", defaults.hitl_enabled),
            auto_approve_default=_bool("AUTO_APPROVE_DEFAULT", defaults.auto_approve_default),
            planner_temperature=_float("PLANNER_TEMPERATURE", defaults.planner_temperature),
            embedding_dim=_int("EMBEDDING_DIM", defaults.embedding_dim),
            rag_top_k=_int("RAG_TOP_K", defaults.rag_top_k),
            rag_min_score=_float("RAG_MIN_SCORE", defaults.rag_min_score),
            rag_chunk_size=_int("RAG_CHUNK_SIZE", defaults.rag_chunk_size),
            rag_chunk_overlap=_int("RAG_CHUNK_OVERLAP", defaults.rag_chunk_overlap),
            sales_db_path=_str("SALES_DB_PATH"),
            knowledge_dir=_str("KNOWLEDGE_DIR"),
            artifacts_dir=_str("ARTIFACTS_DIR"),
            frontend_dist_dir=_str("FRONTEND_DIST_DIR"),
            cors_origins=_list("CORS_ORIGINS", defaults.cors_origins),
            log_level=_str("LOG_LEVEL", defaults.log_level),
            log_json=_bool("LOG_JSON", defaults.log_json),
            langsmith_tracing=_bool("LANGSMITH_TRACING", defaults.langsmith_tracing),
            langsmith_api_key=_str("LANGSMITH_API_KEY"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-wide settings (cached)."""
    return Settings.from_env()


def reload_settings() -> Settings:
    """Clear the settings cache (used by tests / scripts)."""
    get_settings.cache_clear()
    return get_settings()


# Convenient module-level instance for verbose imports (`from app.core.config import settings`).
settings = get_settings()
