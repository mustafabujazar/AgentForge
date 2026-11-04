"""Shared pytest fixtures for the AgentForge test suite.

Test-isolation strategy
-----------------------
* Environment variables are set at **import time of this conftest** — before any
  ``app.*`` module is imported by a test module — so ``app.core.config`` and the
  SQLAlchemy engine created in ``app.db.base`` point at a throwaway SQLite file
  inside a per-session temp directory.  The developer database
  (``data/agentforge.db``) is never touched.
* ``fresh_db`` (autouse, function scope) drops and recreates every table before
  each test and clears process-wide singletons (event broker subscribers/loop,
  LLM provider cache, RAG knowledge store) so tests cannot leak state.
* ``client`` runs FastAPI's lifespan via ``TestClient`` as a context manager, so
  schema bootstrap + demo-user seeding + broker loop binding behave exactly like
  a served app.
* LLM calls are forced offline: ``LLM_PROVIDER=mock`` and blank API keys are set
  here, a session-wide socket guard blocks outbound network connections, and the
  ``fake_llm`` / ``scripted_llm`` fixtures replace the provider factory so every
  ``get_llm()`` call inside the app returns a deterministic scripted fake.
  No test needs pytest-asyncio: async code is driven with ``asyncio.run`` inside
  normal sync tests, and HTTP goes through ``TestClient``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import pytest

# ---------------------------------------------------------------------------
# 1. Environment isolation MUST happen before any ``app.*`` import.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="agentforge-tests-"))
TEST_DB_PATH = _SESSION_TMP / "agentforge_test.db"
TEST_ARTIFACTS_DIR = _SESSION_TMP / "artifacts"
TEST_KNOWLEDGE_DIR = _SESSION_TMP / "knowledge"
TEST_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
TEST_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

TEST_DATABASE_URL = f"sqlite:///{TEST_DB_PATH.as_posix()}"

os.environ.update(
    {
        # --- persistence ---------------------------------------------------
        "DATABASE_URL": TEST_DATABASE_URL,
        "SQLITE_ECHO": "false",
        "ARTIFACTS_DIR": str(TEST_ARTIFACTS_DIR),
        "KNOWLEDGE_DIR": str(TEST_KNOWLEDGE_DIR),
        "SALES_DB_PATH": str(PROJECT_ROOT / "data" / "demo" / "sales.db"),
        # --- LLM: fully offline --------------------------------------------
        "LLM_PROVIDER": "mock",
        "LLM_MODEL": "mock-1",
        "GROQ_API_KEY": "",
        "OPENAI_API_KEY": "",
        "OLLAMA_BASE_URL": "",
        "LANGSMITH_TRACING": "false",
        "LANGSMITH_API_KEY": "",
        # --- deterministic behaviour ---------------------------------------
        "SECRET_KEY": "agentforge-test-secret-key-not-for-production",
        "APP_ENV": "test",
        "HITL_ENABLED": "true",
        "AUTO_APPROVE_DEFAULT": "false",
        "MAX_AGENT_RETRIES": "2",
        "EVAL_PASS_SCORE": "0.72",
        "LLM_MAX_CALLS_PER_RUN": "40",
        "DEMO_USER_EMAIL": "demo@agentforge.io",
        "DEMO_USER_PASSWORD": "demo1234",
        "DEMO_USER_NAME": "Demo Operator",
        # --- quiet logs -----------------------------------------------------
        "LOG_LEVEL": "WARNING",
        "LOG_JSON": "false",
    }
)

DEMO_EMAIL = os.environ["DEMO_USER_EMAIL"]
DEMO_PASSWORD = os.environ["DEMO_USER_PASSWORD"]
DEMO_NAME = os.environ["DEMO_USER_NAME"]

# Long-enough objective for RunCreate (min_length=12).
DEMO_OBJECTIVE = (
    "Analyze our sales performance, identify the three worst-performing products, "
    "investigate why they are underperforming, and generate a report with recommendations."
)


# ---------------------------------------------------------------------------
# 2. Session-scoped safety net + cleanup
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _session_tempdir():
    """Own the session temp dir; dispose the DB engine and delete files at the end."""
    yield _SESSION_TMP
    try:  # best effort: Windows may keep the SQLite WAL file briefly open
        from app.db import base as db_base

        db_base.get_engine().dispose()
    except Exception:  # pragma: no cover - defensive cleanup
        pass
    shutil.rmtree(_SESSION_TMP, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _offline_environment_guard():
    """Fail loudly if the suite was configured against a real LLM or developer DB."""
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.llm_provider == "mock", (
        f"test suite must run with LLM_PROVIDER=mock, got {settings.llm_provider!r}"
    )
    assert not settings.groq_api_key, "GROQ_API_KEY must be blank during tests"
    assert not settings.openai_api_key, "OPENAI_API_KEY must be blank during tests"
    assert settings.is_sqlite, f"tests must use SQLite, got {settings.database_url!r}"
    assert str(TEST_DB_PATH).replace("\\", "/") in settings.database_url, (
        f"tests must use the session temp DB, got {settings.database_url!r}"
    )
    assert not settings.langsmith_tracing, "LangSmith tracing must be disabled during tests"
    yield settings


@pytest.fixture(scope="session", autouse=True)
def _block_network():
    """Block outbound TCP so a misconfigured test can never reach a real provider.

    Loopback connections are allowed: on Windows the asyncio Proactor event loop
    builds its self-pipe with ``socket.socketpair()``, which performs a loopback
    ``connect``.  Remote addresses are always blocked, so no hosted LLM (Groq,
    OpenAI, Ollama on another host) can be reached from a test.
    """
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    loopback_hosts = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}

    def _is_loopback(address) -> bool:  # noqa: ANN001 - socket address shapes
        if isinstance(address, tuple) and address:
            return str(address[0]) in loopback_hosts
        return str(address) in loopback_hosts

    def _blocked_connect(self, address, *args, **kwargs):  # noqa: ANN001 - socket hook
        if _is_loopback(address):
            return original_connect(self, address, *args, **kwargs)
        raise AssertionError(
            f"Network access is blocked in offline tests (attempted connect to {address!r})"
        )

    def _blocked_create_connection(address, *args, **kwargs):  # noqa: ANN001 - socket hook
        if _is_loopback(address):
            return original_create_connection(address, *args, **kwargs)
        raise AssertionError(
            f"Network access is blocked in offline tests (attempted connect to {address!r})"
        )

    socket.socket.connect = _blocked_connect  # type: ignore[method-assign]
    socket.create_connection = _blocked_create_connection  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.create_connection = original_create_connection  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 3. Per-test database + singleton isolation
# ---------------------------------------------------------------------------
def _reset_database() -> None:
    from app.db import base as db_base
    from app.db.models import Base

    engine = db_base.get_engine()
    # Close pooled connections so the file lock is released before DDL on Windows.
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def fresh_db():
    """Fresh schema + clean singletons for every test (autouse)."""
    _reset_database()

    from app.core.events import broker
    from app.llm.registry import reset_llm_cache

    broker.clear()
    broker._loop = None
    reset_llm_cache()
    try:
        from app.rag.store import reset_knowledge_store

        reset_knowledge_store()
    except Exception:  # pragma: no cover - rag package optional while building
        pass
    try:
        from app.api.rag_bridge import reset_rag_bridge

        reset_rag_bridge()
    except Exception:  # pragma: no cover - api package optional while building
        pass
    yield
    try:
        from app.core.events import broker as _broker

        _broker.clear()
        _broker._loop = None
    except Exception:  # pragma: no cover - defensive
        pass


@pytest.fixture
def settings():
    """Process-wide settings (bound to the throwaway test DB / mock provider)."""
    from app.core.config import get_settings

    return get_settings()


@pytest.fixture
def db_session(fresh_db):
    """A SQLAlchemy session against the isolated test database."""
    from app.db.base import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 4. API client + auth fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def app_module():
    """Import the FastAPI application lazily so foundation tests stay independent."""
    import app.main as main_module

    return main_module


@pytest.fixture
def client(fresh_db):
    """FastAPI TestClient with lifespan startup (schema + demo seeding)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _login_or_register(client: Any) -> tuple[str, dict[str, Any]]:
    """Log in as the seeded demo user, registering it if seeding is unavailable."""
    payload = {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    response = client.post("/api/auth/login", json=payload)
    if response.status_code != 200:
        register = client.post(
            "/api/auth/register", json={**payload, "full_name": DEMO_NAME}
        )
        assert register.status_code in (200, 201), (
            "cannot authenticate a demo user; "
            f"login={response.status_code} {response.text!r}; "
            f"register={register.status_code} {register.text!r}"
        )
        response = register
    data = response.json()
    token = data.get("access_token")
    assert token, f"auth response missing access_token: {data!r}"
    return token, data.get("user") or {}


@pytest.fixture
def demo_token(client) -> str:
    """Bearer token for the seeded demo user (``demo@agentforge.io``)."""
    token, _user = _login_or_register(client)
    return token


@pytest.fixture
def auth_headers(demo_token) -> dict[str, str]:
    """``Authorization`` header for the demo user."""
    return {"Authorization": f"Bearer {demo_token}"}


@pytest.fixture
def demo_user(client, auth_headers) -> dict[str, Any]:
    """The authenticated demo user's ``UserRead`` payload."""
    response = client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200, f"/api/auth/me failed: {response.status_code} {response.text!r}"
    return response.json()


# ---------------------------------------------------------------------------
# 5. Scripted / fake LLM (no network)
# ---------------------------------------------------------------------------
# Safe to import: the environment (DATABASE_URL / LLM_PROVIDER) was set above,
# before this module-level import runs.
from app.llm.base import BaseLLM, ChatMessage, LLMResponse, TokenUsage  # noqa: E402


class ScriptedLLM(BaseLLM):
    """Deterministic ``BaseLLM`` driven by a responder callable or a response queue.

    The responder signature matches ``app.llm.mock.Responder``:
    ``responder(messages, json_mode) -> str``.  If no responder is supplied the
    next queued response is returned; otherwise a JSON default is returned for
    ``json_mode=True`` calls and a short acknowledgement otherwise.
    """

    provider = "mock"
    model = "mock-scripted"

    def __init__(
        self,
        responder: Callable[[list[Any], bool], str] | None = None,
        responses: list[str] | None = None,
        default_json: dict[str, Any] | None = None,
    ) -> None:
        self._responder = responder
        self._responses = list(responses or [])
        self._default_json = default_json or {
            "task_id": "task-1",
            "summary": "Scripted LLM completed the task.",
            "findings": [],
            "artifacts": [],
            "confidence": 0.7,
        }
        self.calls: list[dict[str, Any]] = []

    # -- scripting helpers ------------------------------------------------
    def queue(self, *responses: str) -> "ScriptedLLM":
        self._responses.extend(responses)
        return self

    def set_responder(self, responder: Callable[[list[Any], bool], str]) -> "ScriptedLLM":
        self._responder = responder
        return self

    def reset(self) -> None:
        self.calls.clear()
        self._responses.clear()

    # -- stats -------------------------------------------------------------
    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def prompt_tokens(self) -> int:
        return sum(call.get("prompt_tokens", 0) for call in self.calls)

    def prompts(self) -> list[str]:
        out = []
        for call in self.calls:
            for message in call["messages"]:
                if message.get("role") == "user":
                    out.append(message.get("content", ""))
        return out

    def last_user_prompt(self) -> str:
        prompts = self.prompts()
        return prompts[-1] if prompts else ""

    # -- BaseLLM API -------------------------------------------------------
    def invoke(
        self,
        messages: list[Any],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ):
        from app.llm.base import LLMResponse, TokenUsage

        started = time.perf_counter()
        if self._responder is not None:
            content = self._responder(messages, json_mode)
        elif self._responses:
            content = self._responses.pop(0)
        elif json_mode:
            content = json.dumps(self._default_json)
        else:
            last_user = next(
                (m.content for m in reversed(messages) if getattr(m, "role", "") == "user"),
                "",
            )
            content = f"[scripted-llm] acknowledged: {last_user[:160]}"
        if not isinstance(content, str):
            content = json.dumps(content)
        message_dicts = [
            {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}
            for m in messages
        ]
        prompt_tokens = sum(len(m["content"].split()) for m in message_dicts)
        completion_tokens = len(content.split())
        self.calls.append(
            {
                "messages": message_dicts,
                "json_mode": json_mode,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "content": content,
            }
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return LLMResponse(
            content=content,
            model=self.model,
            provider=self.provider,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                model=self.model,
                provider=self.provider,
            ),
            latency_ms=latency_ms,
            finish_reason="stop",
        )


def _install_fake_llm(monkeypatch: pytest.MonkeyPatch, llm: ScriptedLLM) -> None:
    """Make every ``get_llm()`` call in the app resolve to ``llm``.

    Two layers are patched so both historical import styles are covered:
    ``app.llm.registry._build_llm`` (used by the real cached factory, including
    modules that did ``from app.llm import get_llm`` at import time) and the
    ``get_llm`` symbols themselves.
    """
    import app.llm as llm_package
    import app.llm.registry as registry

    original_get_llm = registry.get_llm

    def fake_get_llm(provider=None, model=None, *, force=False):
        return llm

    def fake_build_llm(settings, provider=None, model=None):
        return llm

    registry.reset_llm_cache()
    monkeypatch.setattr(registry, "_build_llm", fake_build_llm)
    monkeypatch.setattr(registry, "get_llm", fake_get_llm)
    monkeypatch.setattr(llm_package, "get_llm", fake_get_llm)
    # Patch modules that imported the symbol directly before this fixture ran.
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if not name.startswith("app."):
            continue
        if getattr(module, "get_llm", None) is original_get_llm:
            monkeypatch.setattr(module, "get_llm", fake_get_llm, raising=False)


@pytest.fixture
def scripted_llm(monkeypatch):
    """Factory: ``scripted_llm(responses=[...])`` or ``scripted_llm(responder=fn)``.

    Each call creates a new :class:`ScriptedLLM` and installs it as the app-wide
    provider via ``monkeypatch`` (the most recent call wins for the test).
    """
    created: list[ScriptedLLM] = []

    def _create(
        responder: Callable[[list[Any], bool], str] | None = None,
        responses: list[str] | None = None,
        default_json: dict[str, Any] | None = None,
    ) -> ScriptedLLM:
        llm = ScriptedLLM(responder=responder, responses=responses, default_json=default_json)
        _install_fake_llm(monkeypatch, llm)
        created.append(llm)
        return llm

    return _create


@pytest.fixture
def fake_llm(scripted_llm) -> ScriptedLLM:
    """A default scripted LLM installed as the app-wide provider."""
    return scripted_llm()


# ``FakeLLM`` is the historical name used by docs; re-export for convenience.
FakeLLM = ScriptedLLM


@pytest.fixture
def no_network(monkeypatch):
    """Hard-fail if any real (OpenAI-compatible) HTTP LLM call is attempted."""
    import app.llm.openai_compat as compat

    def _blocked_invoke(self, *args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("real LLM HTTP call attempted in an offline test")

    monkeypatch.setattr(compat.OpenAICompatibleLLM, "invoke", _blocked_invoke)
    return True


# ---------------------------------------------------------------------------
# 6. Misc helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def wait_for() -> Callable[..., Any]:
    """Poll ``predicate()`` until truthy or timeout (for background workflow threads)."""

    def _wait_for(
        predicate: Callable[[], Any],
        timeout: float = 15.0,
        interval: float = 0.05,
        message: str = "condition",
    ) -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = predicate()
                if last:
                    return last
            except Exception as exc:  # noqa: BLE001 - keep polling; report last error
                last = exc
            time.sleep(interval)
        raise AssertionError(
            f"timed out after {timeout:.1f}s waiting for {message}; last value={last!r}"
        )

    return _wait_for


# ---------------------------------------------------------------------------
# 7. pytest hooks
# ---------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "offline: test must not touch the network")
    config.addinivalue_line(
        "markers", "real_llm: test talks to a real LLM provider (never enabled in CI)"
    )
    config.addinivalue_line("markers", "e2e: end-to-end workflow test")
    config.addinivalue_line("markers", "integration: API integration test")
    config.addinivalue_line("markers", "slow: slower test")
