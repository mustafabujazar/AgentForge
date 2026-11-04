"""Foundation unit tests: security, JWT, JSON extraction, schemas, tool registry,
event broker, settings and the LLM layer.

Everything here is offline, deterministic and fast: no HTTP, no network, no
background workflow threads, no developer database.  The session fixtures in
``tests/conftest.py`` point the app at a temp SQLite file and force
``LLM_PROVIDER=mock`` before the app is imported.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import pytest

from app.core import security
from app.core.errors import LLMConfigurationError, ToolExecutionError
from app.core.security import TokenError
from app.llm.base import (
    ChatMessage,
    LLMError,
    LLMResponse,
    LLMResponseError,
    TokenUsage as LLMTokenUsage,
    extract_json,
)

pytestmark = pytest.mark.offline


# ===========================================================================
# scrypt password hashing
# ===========================================================================
class TestPasswordHashing:
    def test_hash_is_self_describing_scrypt_string(self):
        hashed = security.hash_password("correct horse battery staple")
        parts = hashed.split("$")
        assert len(parts) == 6
        scheme, n, r, p, salt, digest = parts
        assert scheme == "scrypt"
        assert (int(n), int(r), int(p)) == (2**14, 8, 1)
        # urlsafe base64 without padding
        assert "=" not in salt and "=" not in digest
        assert len(digest) == 43  # 32 bytes -> 43 urlsafe-b64 chars

    def test_verify_accepts_correct_password(self):
        hashed = security.hash_password("s3cret-pässword")
        assert security.verify_password("s3cret-pässword", hashed) is True

    def test_verify_rejects_wrong_password(self):
        hashed = security.hash_password("s3cret")
        assert security.verify_password("s3cret ", hashed) is False
        assert security.verify_password("", hashed) is False

    def test_salted_hashes_are_unique_and_both_verify(self):
        first = security.hash_password("same-password")
        second = security.hash_password("same-password")
        assert first != second
        assert security.verify_password("same-password", first)
        assert security.verify_password("same-password", second)

    @pytest.mark.parametrize(
        "bad_hash",
        [
            "",
            "not-a-hash",
            "scrypt$only$five$fields",
            "argon2$16384$8$1$c2FsdA$aGFzaA",
            "scrypt$notanint$8$1$c2FsdA$aGFzaA",
            "scrypt$16384$8$1$!!!$aGFzaA",
        ],
    )
    def test_verify_returns_false_for_malformed_hashes(self, bad_hash):
        assert security.verify_password("whatever", bad_hash) is False

    def test_hash_rejects_empty_password(self):
        with pytest.raises(ValueError):
            security.hash_password("")

    def test_new_id_is_32_hex_chars(self):
        value = security.new_id()
        assert len(value) == 32
        int(value, 16)  # hex-parseable
        assert security.new_id() != value


# ===========================================================================
# HS256 JWT round-trip and rejection
# ===========================================================================
class TestJWT:
    def _split(self, token: str) -> tuple[str, str, str]:
        header, payload, signature = token.split(".")
        return header, payload, signature

    def test_round_trip_contains_expected_claims(self):
        token = security.create_access_token("user-123", role="admin")
        claims = security.decode_access_token(token)
        assert claims["sub"] == "user-123"
        assert claims["role"] == "admin"
        assert isinstance(claims["jti"], str) and len(claims["jti"]) == 16
        assert claims["iat"] <= int(time.time())
        assert claims["exp"] > int(time.time())

    def test_default_role_and_expiry(self, settings):
        before = int(time.time())
        token = security.create_access_token("subject")
        claims = security.decode_access_token(token)
        assert claims["role"] == "member"
        assert claims["exp"] == pytest.approx(
            before + settings.access_token_expire_minutes * 60, abs=5
        )

    def test_extra_claims_are_merged(self):
        token = security.create_access_token(
            "user-1", extra_claims={"scope": "runs:write", "team": "qa"}
        )
        claims = security.decode_access_token(token)
        assert claims["scope"] == "runs:write"
        assert claims["team"] == "qa"

    def test_expired_token_is_rejected(self):
        token = security.create_access_token("user-1", expires_minutes=-1)
        with pytest.raises(TokenError, match="expired"):
            security.decode_access_token(token)

    def test_expiry_boundary_just_before_now_is_expired(self):
        token = security.create_access_token("user-1", expires_minutes=0)
        # expires_minutes=0 is falsy -> falls back to the configured default.
        assert security.decode_access_token(token)["sub"] == "user-1"
        # Clock-independent: 30s in the past can never satisfy int(exp) >= int(now).
        expired = security.create_access_token("user-1", expires_minutes=-0.5)
        with pytest.raises(TokenError):
            security.decode_access_token(expired)

    def test_tampered_signature_is_rejected(self):
        header, payload, signature = self._split(security.create_access_token("user-1"))
        flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
        with pytest.raises(TokenError, match="signature"):
            security.decode_access_token(f"{header}.{payload}.{flipped}")

    def test_tampered_payload_is_rejected(self):
        header, payload, signature = self._split(security.create_access_token("user-1"))
        forged = security._b64url_encode(
            json.dumps({"sub": "someone-else", "exp": int(time.time()) + 600}).encode()
        )
        with pytest.raises(TokenError, match="signature"):
            security.decode_access_token(f"{header}.{forged}.{signature}")

    @pytest.mark.parametrize("bad_token", ["", "abc", "a.b", "a.b.c.d", "..", "abc.def.ghi"])
    def test_malformed_tokens_are_rejected(self, bad_token):
        with pytest.raises(TokenError):
            security.decode_access_token(bad_token)


# ===========================================================================
# extract_json robustness
# ===========================================================================
class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}

    def test_markdown_json_fence(self):
        text = 'Here you go:\n```json\n{"objective": "grow", "tasks": [1, 2]}\n```\nEnjoy.'
        assert extract_json(text) == {"objective": "grow", "tasks": [1, 2]}

    def test_markdown_bare_fence(self):
        text = "```\n{\"ok\": true}\n```"
        assert extract_json(text) == {"ok": True}

    def test_preamble_and_suffix_text(self):
        text = 'Sure! The plan is: {"tasks": [{"id": "t1"}]} -- tell me if you need more.'
        assert extract_json(text) == {"tasks": [{"id": "t1"}]}

    def test_trailing_commas_are_tolerated(self):
        text = '{"a": 1, "b": [1, 2, 3,], "c": {"d": 4,},}'
        assert extract_json(text) == {"a": 1, "b": [1, 2, 3], "c": {"d": 4}}

    def test_nested_objects_and_arrays(self):
        payload = {"plan": {"tasks": [{"id": "task-1", "depends_on": []}]}, "n": 2}
        assert extract_json(json.dumps(payload)) == payload

    def test_braces_inside_strings_do_not_break_brace_matching(self):
        text = '{"query": "SELECT * FROM t WHERE a = \'{\' AND b = \'}\'", "n": 1}'
        parsed = extract_json(text)
        assert parsed["n"] == 1
        assert "SELECT" in parsed["query"]

    def test_escaped_quotes_inside_strings(self):
        text = '{"message": "she said \\"hi\\" loudly", "n": 2}'
        parsed = extract_json(text)
        assert parsed["message"] == 'she said "hi" loudly'
        assert parsed["n"] == 2

    def test_single_quoted_payload_fallback(self):
        assert extract_json("{'a': 1, 'b': 'x'}") == {"a": 1, "b": "x"}

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   \n\t ",
            "I cannot help with that.",
            "[1, 2, 3]",
            '"just a string"',
        ],
    )
    def test_non_object_or_empty_input_raises(self, text):
        with pytest.raises(LLMResponseError):
            extract_json(text)

    def test_unbalanced_object_raises(self):
        with pytest.raises(LLMResponseError, match="unbalanced"):
            extract_json('{"a": 1, "b": {"c": 2}')

    def test_garbage_object_raises(self):
        with pytest.raises(LLMResponseError):
            extract_json('{"a": 1,, "b": }')


# ===========================================================================
# Workflow schemas: aliases, validation, duplicate ids, enums
# ===========================================================================
def _task(**overrides):
    payload = {
        "id": "task-1",
        "title": "Analyze revenue",
        "description": "Look at sales data",
        "agent": "analyst",
    }
    payload.update(overrides)
    return payload


class TestTaskSpecAndPlan:
    @pytest.mark.parametrize(
        ("raw_agent", "expected"),
        [
            ("analyst", "analyst"),
            ("data_analyst", "analyst"),
            ("Data Analyst", "analyst"),
            ("analytics", "analyst"),
            ("data", "analyst"),
            ("research", "research"),
            ("Researcher", "research"),
            ("research-agent", "research"),
            ("execution", "execution"),
            ("executor", "execution"),
            ("operations", "execution"),
            ("reporting", "reporting"),
            ("reporter", "reporting"),
            ("writer", "reporting"),
            ("report", "reporting"),
            ("planner", "planner"),
            ("orchestrator", "planner"),
        ],
    )
    def test_agent_aliases_normalize(self, raw_agent, expected):
        from app.schemas.workflow import TaskSpec

        assert TaskSpec(**_task(agent=raw_agent)).agent.value == expected

    def test_agent_enum_value_is_accepted(self):
        from app.schemas.workflow import AgentRole, TaskSpec

        task = TaskSpec(**_task(agent=AgentRole.EXECUTION))
        assert task.agent is AgentRole.EXECUTION

    def test_unknown_agent_is_rejected(self):
        from pydantic import ValidationError

        from app.schemas.workflow import TaskSpec

        with pytest.raises(ValidationError):
            TaskSpec(**_task(agent="sales_bot"))

    def test_task_id_normalization(self):
        from app.schemas.workflow import TaskSpec

        assert TaskSpec(**_task(id="  Task 1  ")).id == "task-1"
        assert TaskSpec(**_task(id="")).id == "task"

    def test_priority_bounds(self):
        from pydantic import ValidationError

        from app.schemas.workflow import TaskSpec

        assert TaskSpec(**_task(priority=1)).priority == 1
        assert TaskSpec(**_task(priority=5)).priority == 5
        with pytest.raises(ValidationError):
            TaskSpec(**_task(priority=0))
        with pytest.raises(ValidationError):
            TaskSpec(**_task(priority=6))

    def test_defaults_and_extra_ignore(self):
        from app.schemas.workflow import TaskSpec

        task = TaskSpec(**_task(surprise="ignored", sensitive=True))
        assert task.sensitive is True
        assert task.depends_on == []
        assert task.tool_hints == []
        assert task.success_criteria == []
        assert task.expected_output == ""
        assert not hasattr(task, "surprise")

    def test_plan_round_trip(self):
        from app.schemas.workflow import Plan

        plan = Plan(
            objective="Find the worst products",
            summary="Three parallel analyses",
            tasks=[_task(id="task-1"), _task(id="task-2", agent="research")],
            success_criteria=["names three products"],
            requires_approval=False,
        )
        dumped = plan.model_dump(mode="json")
        assert [task["id"] for task in dumped["tasks"]] == ["task-1", "task-2"]
        assert Plan.model_validate(dumped) == plan
        assert plan.task_map()["task-2"].agent.value == "research"

    def test_duplicate_task_ids_are_deduped(self):
        from app.schemas.workflow import Plan

        plan = Plan(
            objective="x",
            tasks=[
                _task(id="task-1"),
                _task(id="task-1", title="duplicate"),
                _task(id="task-2"),
            ],
        )
        ids = [task.id for task in plan.tasks]
        assert len(set(ids)) == len(ids) == 3
        assert ids[0] == "task-1"

    def test_plan_requires_at_least_one_task(self):
        from pydantic import ValidationError

        from app.schemas.workflow import Plan

        with pytest.raises(ValidationError):
            Plan(objective="x", tasks=[])

    def test_plan_limits_task_count(self):
        from pydantic import ValidationError

        from app.schemas.workflow import Plan

        too_many = [_task(id=f"task-{i}") for i in range(1, 10)]
        with pytest.raises(ValidationError):
            Plan(objective="x", tasks=too_many)

    def test_plan_requires_objective(self):
        from pydantic import ValidationError

        from app.schemas.workflow import Plan

        with pytest.raises(ValidationError):
            Plan(tasks=[_task()])

    def test_evaluation_and_finding_validation(self):
        from pydantic import ValidationError

        from app.schemas.workflow import Evaluation, Finding

        evaluation = Evaluation(task_id="task-1", score=0.9, passed=True, issues=[])
        assert evaluation.evaluator == "llm+rules"
        with pytest.raises(ValidationError):
            Evaluation(task_id="task-1", score=1.5)
        with pytest.raises(ValidationError):
            Evaluation(task_id="task-1", score=-0.1)
        finding = Finding(statement="Revenue fell 12%", confidence=0.8)
        assert finding.confidence == pytest.approx(0.8)
        with pytest.raises(ValidationError):
            Finding(statement="x", confidence=2.0)


class TestEnums:
    def test_agent_roles(self):
        from app.schemas.workflow import AgentRole

        assert {role.value for role in AgentRole} == {
            "planner",
            "research",
            "analyst",
            "execution",
            "reporting",
        }
        assert AgentRole("analyst") is AgentRole.ANALYST

    @pytest.mark.parametrize(
        ("status", "terminal"),
        [
            ("queued", False),
            ("running", False),
            ("awaiting_approval", False),
            ("completed", True),
            ("failed", True),
            ("cancelled", True),
        ],
    )
    def test_run_status_terminal(self, status, terminal):
        from app.schemas.workflow import RunStatus

        assert RunStatus(status).terminal is terminal

    def test_task_status_values(self):
        from app.schemas.workflow import TaskStatus

        assert {status.value for status in TaskStatus} == {
            "pending",
            "running",
            "completed",
            "failed",
            "retrying",
            "awaiting_approval",
            "skipped",
        }

    def test_token_usage_add(self):
        from app.schemas.workflow import TokenUsage

        first = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, calls=1, model="m")
        second = TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5, calls=1, provider="mock")
        combined = first.add(second)
        assert (combined.prompt_tokens, combined.completion_tokens, combined.total_tokens) == (13, 7, 20)
        assert combined.calls == 2
        assert combined.model == "m"
        assert combined.provider == "mock"


# ===========================================================================
# Tool registry: registration, validation, scopes, sensitive tools
# ===========================================================================
class _EchoTool:
    """Factory helper to build fresh tool instances (avoids subclassing BaseTool at import)."""


def _make_tool_classes():
    from app.tools.base import BaseTool, ToolResult, ToolSpec

    class EchoTool(BaseTool):
        spec = ToolSpec(
            name="echo",
            description="Echo back the provided text",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "count": {"type": "integer"},
                    "ratio": {"type": "number"},
                    "flag": {"type": "boolean"},
                    "items": {"type": "array"},
                    "payload": {"type": "object"},
                },
                "required": ["text"],
            },
            scopes=("read:data",),
            category="test",
        )

        def __init__(self):
            self.calls = []

        def run(self, args, ctx):
            self.calls.append((args, ctx))
            return ToolResult.success({"echo": args["text"], "agent": ctx.agent})

    class SensitiveTool(BaseTool):
        spec = ToolSpec(
            name="send_email",
            description="Send an email (side effect)",
            input_schema={"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]},
            scopes=("write:email",),
            sensitive=True,
            category="action",
        )

        def run(self, args, ctx):
            return ToolResult.success(f"emailed {args['to']}")

    class BoomTool(BaseTool):
        spec = ToolSpec(name="boom", description="Always raises", input_schema={})

        def run(self, args, ctx):
            raise ValueError("boom")

    class DomainFailTool(BaseTool):
        spec = ToolSpec(name="domain_fail", description="Raises a domain error", input_schema={})

        def run(self, args, ctx):
            raise ToolExecutionError("tool boom")

    class RawOutputTool(BaseTool):
        spec = ToolSpec(name="raw", description="Returns a raw value", input_schema={})

        def run(self, args, ctx):
            return {"raw": True}

    return EchoTool, SensitiveTool, BoomTool, DomainFailTool, RawOutputTool


@pytest.fixture
def tools():
    """(registry, tool instances, ToolContext) tuple for registry tests.

    Uses a ``ToolRegistry`` subclass whose ``ensure_defaults`` is disabled so the
    concrete application tools (loaded lazily by the real registry) never leak
    into these isolated assertions.
    """
    from app.tools.base import ToolContext
    from app.tools.registry import ToolRegistry

    class _IsolatedRegistry(ToolRegistry):
        def ensure_defaults(self) -> None:  # noqa: D401 - test double
            return

    EchoTool, SensitiveTool, BoomTool, DomainFailTool, RawOutputTool = _make_tool_classes()
    registry = _IsolatedRegistry()
    instances = {
        "echo": EchoTool(),
        "sensitive": SensitiveTool(),
        "boom": BoomTool(),
        "domain_fail": DomainFailTool(),
        "raw": RawOutputTool(),
    }
    registry.register(instances["echo"])
    registry.register(instances["sensitive"])
    registry.register(instances["boom"])
    registry.register(instances["domain_fail"])
    registry.register(instances["raw"])
    ctx = ToolContext(run_id="run-1", agent="analyst")
    return registry, instances, ctx


class TestToolRegistry:
    def test_register_and_list(self, tools):
        registry, instances, _ctx = tools
        names = registry.tool_names()
        assert names == ["boom", "domain_fail", "echo", "raw", "send_email"]
        assert registry.get("echo") is instances["echo"]
        assert registry.require("echo") is instances["echo"]
        assert registry.get("missing") is None
        with pytest.raises(ToolExecutionError, match="unknown tool"):
            registry.require("missing")
        specs = registry.list_specs()
        assert len(specs) == 5
        echo_spec = next(spec for spec in specs if spec.name == "echo")
        assert echo_spec.category == "test"
        assert echo_spec.scopes == ("read:data",)
        assert echo_spec.sensitive is False
        assert "input_schema" in echo_spec.to_dict()

    def test_to_dicts_are_json_serializable(self, tools):
        registry, _instances, _ctx = tools
        payload = registry.to_dicts()
        json.dumps(payload)
        echo = next(item for item in payload if item["name"] == "echo")
        assert echo["scopes"] == ["read:data"]
        assert echo["sensitive"] is False

    def test_duplicate_registration_replaces_in_place(self, tools):
        registry, instances, _ctx = tools
        registry.register(instances["echo"])
        assert len(registry.list_specs()) == 5

    def test_register_rejects_non_tool(self, tools):
        registry, _instances, _ctx = tools
        with pytest.raises(TypeError):
            registry.register(object())

    def test_register_rejects_unnamed_tool(self):
        from app.tools.base import BaseTool, ToolResult, ToolSpec
        from app.tools.registry import ToolRegistry

        class NamelessTool(BaseTool):
            spec = ToolSpec(name="", description="")

            def run(self, args, ctx):
                return ToolResult.success(None)

        with pytest.raises(ValueError):
            ToolRegistry().register(NamelessTool())

    def test_execute_unknown_tool_returns_failure(self, tools):
        registry, _instances, ctx = tools
        result = registry.execute("nope", {}, ctx)
        assert result.ok is False
        assert "unknown tool" in result.error

    def test_execute_without_context_returns_failure(self, tools):
        registry, _instances, _ctx = tools
        result = registry.execute("echo", {"text": "hi"})
        assert result.ok is False
        assert "context is required" in result.error

    def test_execute_success_returns_output_and_context(self, tools):
        registry, instances, ctx = tools
        result = registry.execute("echo", {"text": "hello"}, ctx)
        assert result.ok is True
        assert result.output["echo"] == "hello"
        assert result.output["agent"] == "analyst"
        assert result.error is None
        assert isinstance(result.duration_ms, int)
        assert instances["echo"].calls[0][1] is ctx

    def test_missing_required_argument(self, tools):
        registry, _instances, ctx = tools
        result = registry.execute("echo", {}, ctx)
        assert result.ok is False
        assert "missing required argument: text" in result.error

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ({"text": 5}, "argument 'text' must be a string"),
            ({"text": "ok", "count": "3"}, "argument 'count' must be a integer"),
            ({"text": "ok", "count": True}, "argument 'count' must be a integer"),
            ({"text": "ok", "ratio": "1.5"}, "argument 'ratio' must be a number"),
            ({"text": "ok", "flag": "yes"}, "argument 'flag' must be a boolean"),
            ({"text": "ok", "items": "not-a-list"}, "argument 'items' must be a array"),
            ({"text": "ok", "payload": []}, "argument 'payload' must be a object"),
        ],
    )
    def test_type_validation(self, tools, args, message):
        registry, _instances, ctx = tools
        result = registry.execute("echo", args, ctx)
        assert result.ok is False
        assert message in result.error

    def test_valid_numeric_types(self, tools):
        registry, _instances, ctx = tools
        assert registry.execute("echo", {"text": "x", "count": 2, "ratio": 1}, ctx).ok is True
        assert registry.execute("echo", {"text": "x", "ratio": 1.5}, ctx).ok is True
        assert registry.execute("echo", {"text": "x", "flag": True}, ctx).ok is True

    def test_scope_denial(self, tools):
        registry, _instances, ctx = tools
        denied = registry.execute("echo", {"text": "hi"}, ctx, allowed_scopes={"read:other"})
        assert denied.ok is False
        assert "scopes not granted" in denied.error
        assert "read:data" in denied.error

        allowed = registry.execute("echo", {"text": "hi"}, ctx, allowed_scopes={"read:data", "write:x"})
        assert allowed.ok is True

    def test_no_scope_check_when_allowed_scopes_is_none(self, tools):
        registry, _instances, ctx = tools
        assert registry.execute("echo", {"text": "hi"}, ctx).ok is True

    def test_sensitive_tool_requires_approval(self, tools):
        registry, _instances, ctx = tools
        denied = registry.execute("send_email", {"to": "a@b.c"}, ctx)
        assert denied.ok is False
        assert "sensitive" in denied.error
        assert "approval" in denied.error

        approved_ctx = type(ctx)(run_id=ctx.run_id, agent=ctx.agent, extras={"approval_granted": True})
        granted = registry.execute("send_email", {"to": "a@b.c"}, approved_ctx)
        assert granted.ok is True
        assert granted.output == "emailed a@b.c"

    def test_tool_exception_is_converted_to_failure(self, tools):
        registry, _instances, ctx = tools
        result = registry.execute("boom", {}, ctx)
        assert result.ok is False
        assert result.error == "ValueError: boom"

    def test_domain_error_is_converted_to_failure(self, tools):
        registry, _instances, ctx = tools
        result = registry.execute("domain_fail", {}, ctx)
        assert result.ok is False
        assert result.error == "tool boom"

    def test_raw_tool_output_is_wrapped(self, tools):
        registry, _instances, ctx = tools
        result = registry.execute("raw", {}, ctx)
        assert result.ok is True
        assert result.output == {"raw": True}

    def test_clear_resets_registry(self, tools):
        registry, _instances, _ctx = tools
        registry.clear()
        assert registry.list_specs() == []
        assert registry.get("echo") is None

    def test_global_registry_is_singleton_and_lazy(self):
        from app.tools.registry import ToolRegistry, get_registry, reset_registry

        reset_registry()
        first = get_registry()
        second = get_registry()
        assert first is second
        assert isinstance(first, ToolRegistry)
        # Concrete tools may still be landing; every registered spec must be coherent.
        for spec in first.list_specs():
            assert spec.name
            assert spec.description
            assert isinstance(spec.input_schema, dict)
            assert isinstance(spec.scopes, tuple)


class TestToolBaseTypes:
    def test_tool_result_helpers(self):
        from app.tools.base import ToolResult

        ok = ToolResult.success({"a": 1}, meta={"m": 2})
        assert ok.ok is True and ok.error is None and ok.meta == {"m": 2}
        failed = ToolResult.failure("nope", duration_ms=7)
        assert failed.ok is False and failed.error == "nope" and failed.duration_ms == 7
        preview = ToolResult.success("x" * 5000).preview(limit=100)
        assert len(preview) == 100

    def test_tool_context_resolved_artifacts_dir(self, settings):
        from app.tools.base import ToolContext

        ctx = ToolContext(run_id="run-1", agent="analyst")
        path = ctx.resolved_artifacts_dir
        assert path.exists() and path.is_dir()
        assert path == settings.artifacts_path


# ===========================================================================
# Event broker (pub/sub, cleanup, thread safety)
# ===========================================================================
class TestEventBroker:
    def test_publish_requires_subscribers_only(self):
        from app.core.events import EventBroker

        broker = EventBroker()
        broker.publish_threadsafe("run-1", {"type": "run.started"})  # no subscribers, no error
        assert broker.subscriber_count("run-1") == 0

    def test_async_pubsub_and_subscriber_cleanup(self):
        from app.core.events import EventBroker

        async def scenario():
            broker = EventBroker()
            assert broker.subscriber_count("run-1") == 0
            generator = broker.subscribe("run-1")
            pending = asyncio.create_task(generator.__anext__())
            for _ in range(200):
                if broker.subscriber_count("run-1") == 1:
                    break
                await asyncio.sleep(0.005)
            assert broker.subscriber_count("run-1") == 1

            await broker.publish("run-1", {"seq": 1, "run_id": "run-1", "type": "run.started"})
            event = await asyncio.wait_for(pending, timeout=2)
            assert event["seq"] == 1 and event["type"] == "run.started"

            await generator.aclose()
            for _ in range(200):
                if broker.subscriber_count("run-1") == 0:
                    break
                await asyncio.sleep(0.005)
            assert broker.subscriber_count("run-1") == 0

        asyncio.run(scenario())

    def test_multiple_subscribers_receive_same_event(self):
        from app.core.events import EventBroker

        async def scenario():
            broker = EventBroker()
            generators = [broker.subscribe("run-2") for _ in range(3)]
            pending = [asyncio.create_task(gen.__anext__()) for gen in generators]
            for _ in range(200):
                if broker.subscriber_count("run-2") == 3:
                    break
                await asyncio.sleep(0.005)
            assert broker.subscriber_count("run-2") == 3

            await broker.publish("run-2", {"seq": 5, "type": "task.started"})
            events = await asyncio.wait_for(asyncio.gather(*pending), timeout=2)
            assert [event["seq"] for event in events] == [5, 5, 5]
            for gen in generators:
                await gen.aclose()

        asyncio.run(scenario())

    def test_publish_threadsafe_from_worker_thread(self):
        from app.core.events import EventBroker

        async def scenario():
            broker = EventBroker()
            broker.bind_loop()
            assert broker._loop is asyncio.get_running_loop()
            generator = broker.subscribe("run-3")

            received: list[dict] = []

            async def consume():
                async for event in generator:
                    received.append(event)
                    if len(received) == 2:
                        return

            consumer = asyncio.create_task(consume())
            for _ in range(200):
                if broker.subscriber_count("run-3") == 1:
                    break
                await asyncio.sleep(0.005)

            def worker():
                broker.publish_threadsafe("run-3", {"seq": 1, "type": "task.started"})
                broker.publish_threadsafe("run-3", {"seq": 2, "type": "task.completed"})

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive()

            await asyncio.wait_for(consumer, timeout=2)
            assert [event["seq"] for event in received] == [1, 2]
            await generator.aclose()

        asyncio.run(scenario())

    def test_clear_drops_every_subscriber(self):
        from app.core.events import EventBroker

        async def scenario():
            broker = EventBroker()
            generator = broker.subscribe("run-4")
            pending = asyncio.create_task(generator.__anext__())
            for _ in range(200):
                if broker.subscriber_count("run-4") == 1:
                    break
                await asyncio.sleep(0.005)
            broker.clear()
            assert broker.subscriber_count("run-4") == 0
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

        asyncio.run(scenario())


# ===========================================================================
# Settings and LLM layer
# ===========================================================================
class TestSettings:
    def test_dataclass_defaults(self):
        from app.core.config import Settings

        defaults = Settings()
        assert defaults.app_name == "AgentForge"
        assert defaults.app_version == "1.0.0"
        assert defaults.environment == "development"
        assert defaults.demo_user_email == "demo@agentforge.io"
        assert defaults.max_agent_retries == 2
        assert defaults.eval_pass_score == pytest.approx(0.72)
        assert defaults.hitl_enabled is True
        assert defaults.auto_approve_default is False
        assert defaults.step_timeout_seconds == 240
        assert defaults.rag_top_k == 5
        assert defaults.embedding_dim == 384
        assert defaults.is_sqlite is False  # empty database_url
        assert defaults.sales_db.name == "sales.db"
        assert defaults.artifacts_path.name == "artifacts"
        assert "http://localhost:5173" in defaults.cors_origins

    def test_from_env_reads_test_configuration(self, settings):
        assert settings.llm_provider == "mock"
        assert settings.environment == "test"
        assert settings.database_url.startswith("sqlite:///")
        assert settings.hitl_enabled is True
        assert settings.auto_approve_default is False
        assert settings.max_agent_retries == 2
        assert settings.eval_pass_score == pytest.approx(0.72)
        assert settings.demo_user_email == "demo@agentforge.io"
        assert settings.log_json is False

    def test_get_settings_is_cached(self):
        from app.core.config import get_settings

        assert get_settings() is get_settings()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("postgres://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
            ("postgresql://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
            ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
            ("sqlite:///tmp/x.db", "sqlite:///tmp/x.db"),
        ],
    )
    def test_database_url_normalization(self, raw, expected):
        from app.core.config import _normalize_database_url

        assert _normalize_database_url(raw) == expected

    def test_sqlite_detection(self):
        from app.core.config import Settings

        assert Settings(database_url="sqlite:///x.db").is_sqlite is True
        assert Settings(database_url="postgresql+psycopg://u:p@h/db").is_sqlite is False


class TestLLMLayer:
    def test_mock_provider_is_default_and_cached(self):
        from app.llm import MockLLM, get_llm, reset_llm_cache

        reset_llm_cache()
        llm = get_llm()
        assert isinstance(llm, MockLLM)
        assert get_llm() is llm

    def test_groq_without_key_raises_configuration_error(self):
        from app.llm import get_llm, reset_llm_cache

        reset_llm_cache()
        with pytest.raises(LLMConfigurationError):
            get_llm(provider="groq")

    def test_unknown_provider_raises_configuration_error(self):
        from app.llm import get_llm, reset_llm_cache

        reset_llm_cache()
        with pytest.raises(LLMConfigurationError):
            get_llm(provider="does-not-exist")

    def test_provider_info_for_mock_and_unconfigured_groq(self):
        from app.core.config import Settings
        from app.llm.registry import provider_info

        mock_info = provider_info(Settings(llm_provider="mock"))
        assert mock_info["provider"] == "mock"
        assert mock_info["configured"] is True

        groq_info = provider_info(Settings(llm_provider="groq", groq_api_key=""))
        assert groq_info["provider"] == "groq"
        assert groq_info["configured"] is False

        ollama_info = provider_info(Settings(llm_provider="ollama"))
        assert ollama_info["provider"] == "ollama"
        assert ollama_info["model"] == Settings().ollama_model

    def test_mock_llm_scripted_responses_and_calls(self):
        from app.llm import MockLLM

        llm = MockLLM(responses=["first", "second"])
        response = llm.invoke([ChatMessage("user", "hello there")])
        assert isinstance(response, LLMResponse)
        assert response.content == "first"
        assert response.provider == "mock" and response.model == "mock-1"
        assert response.usage.total_tokens > 0
        assert response.usage.prompt_tokens > 0
        assert response.finish_reason == "stop"
        assert llm.calls[0]["messages"] == [{"role": "user", "content": "hello there"}]
        assert llm.invoke([ChatMessage("user", "again")]).content == "second"

    def test_mock_llm_json_mode_default(self):
        from app.llm import MockLLM, extract_json

        llm = MockLLM()
        response = llm.invoke([ChatMessage("user", "plan it")], json_mode=True)
        parsed = extract_json(response.content)
        assert parsed["task_id"] == "task-1"
        assert llm.calls[0]["json_mode"] is True

    def test_mock_llm_queue_helper(self):
        from app.llm import MockLLM

        llm = MockLLM()
        llm.queue("a", "b")
        assert llm.invoke([ChatMessage("user", "1")]).content == "a"
        assert llm.invoke([ChatMessage("user", "2")]).content == "b"

    def test_mock_llm_responder_receives_json_mode(self):
        from app.llm import MockLLM

        seen: list[bool] = []

        def responder(messages, json_mode):
            seen.append(json_mode)
            return '{"ok": true}'

        llm = MockLLM(responder=responder)
        assert llm.invoke([ChatMessage("user", "x")], json_mode=True).content == '{"ok": true}'
        assert seen == [True]

    def test_fallback_llm_uses_next_model_after_provider_error(self):
        from app.llm.base import BaseLLM
        from app.llm.fallback import FallbackLLM

        class StubLLM(BaseLLM):
            provider = "groq"

            def __init__(self, model, error=None):
                self.model = model
                self.error = error
                self.calls = 0

            def invoke(self, messages, *, temperature=None, max_tokens=None, json_mode=False):
                self.calls += 1
                if self.error:
                    raise self.error
                return LLMResponse(
                    content="ok",
                    model=self.model,
                    provider=self.provider,
                    usage=LLMTokenUsage(model=self.model, provider=self.provider),
                )

        primary = StubLLM("primary", LLMResponseError("model rejected JSON"))
        fallback = StubLLM("fallback")
        chain = FallbackLLM([primary, fallback])

        response = chain.invoke([ChatMessage("user", "hello")], json_mode=True)

        assert response.content == "ok"
        assert response.model == "fallback"
        assert primary.calls == fallback.calls == 1
        assert chain.health()["fallback_models"] == ["fallback"]

    def test_fallback_llm_raises_when_all_models_fail(self):
        from app.llm.base import BaseLLM
        from app.llm.fallback import FallbackLLM

        class FailingLLM(BaseLLM):
            provider = "groq"

            def __init__(self, model):
                self.model = model

            def invoke(self, messages, *, temperature=None, max_tokens=None, json_mode=False):
                raise LLMResponseError(f"{self.model} unavailable")

        chain = FallbackLLM([FailingLLM("primary"), FailingLLM("fallback")])

        with pytest.raises(LLMError, match="primary.*fallback"):
            chain.invoke([ChatMessage("user", "hello")])

    def test_llm_token_usage_add(self):
        first = LLMTokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3, model="a", provider="mock")
        second = LLMTokenUsage(prompt_tokens=4, completion_tokens=5, total_tokens=9, model="b", provider="groq")
        combined = first.add(second)
        assert (combined.prompt_tokens, combined.completion_tokens, combined.total_tokens) == (5, 7, 12)
        assert combined.model == "b"  # precedence: other wins when set
        assert combined.provider == "groq"

    def test_openai_compatible_payload_and_headers(self):
        from app.llm.openai_compat import OpenAICompatibleLLM

        llm = OpenAICompatibleLLM(
            provider="groq",
            model="openai/gpt-oss-20b",
            base_url="https://api.groq.com/openai/v1/",
            api_key="test-key",
        )
        assert llm.base_url == "https://api.groq.com/openai/v1"
        headers = llm._headers()
        assert headers["Authorization"] == "Bearer test-key"
        payload = llm._payload([ChatMessage("user", "hi")], 0.1, 100, json_mode=True)
        assert payload["model"] == "openai/gpt-oss-20b"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 100
        assert payload["temperature"] == pytest.approx(0.1)

        ollama_style = OpenAICompatibleLLM(
            provider="ollama",
            model="llama3.1",
            base_url="http://localhost:11434/v1",
            api_key="ollama",
            json_mode_style="format",
        )
        assert ollama_style._payload([], 0.2, None, json_mode=True)["format"] == "json"

    def test_openai_compatible_empty_messages_raises_before_network(self):
        from app.llm.openai_compat import OpenAICompatibleLLM

        llm = OpenAICompatibleLLM(
            provider="groq", model="m", base_url="https://example.invalid", api_key="k"
        )
        with pytest.raises(LLMError, match="no messages"):
            llm.invoke([])

    def test_llm_health_shape(self):
        from app.llm import MockLLM

        health = MockLLM().health()
        assert health == {"provider": "mock", "model": "mock-1", "configured": True}


class TestEvidenceGrounding:
    def test_plan_does_not_block_independent_sql_work_on_research(self):
        from app.orchestrator.router import normalize_plan
        from app.schemas.workflow import AgentRole, Plan, TaskSpec

        plan = Plan(
            objective="Analyze sales and investigate root causes",
            tasks=[
                TaskSpec(
                    id="task-1",
                    title="Research internal context",
                    description="Find relevant memos.",
                    agent=AgentRole.RESEARCH,
                ),
                TaskSpec(
                    id="task-2",
                    title="Rank products by revenue",
                    description="Query sales data.",
                    agent=AgentRole.ANALYST,
                    depends_on=["task-1"],
                    tool_hints=["sales_sql"],
                ),
            ],
        )

        normalized = normalize_plan(plan)

        assert normalized.task_map()["task-2"].depends_on == []

    def test_analyst_rejects_quantified_claims_absent_from_tool_output(self):
        from types import SimpleNamespace

        from app.orchestrator.evaluator import Evaluator
        from app.schemas.workflow import AgentResult, Finding, TaskSpec, ToolCallRecord

        task = TaskSpec(
            id="task-1",
            title="Find the lowest-revenue products",
            description="Use sales data to identify the bottom three.",
            agent="analyst",
        )
        result = AgentResult(
            task_id="task-1",
            agent="analyst",
            summary="The three lowest-revenue products were identified from sales data.",
            findings=[
                Finding(
                    statement="Product 8 revenue was $12,340.",
                    evidence="sales_sql returned revenue $12,340 for product 8.",
                    source="sales_sql",
                ),
                Finding(
                    statement="Product 8 was among the lowest-revenue products.",
                    evidence="sales_sql output was reviewed.",
                    source="sales_sql",
                ),
            ],
            tool_calls=[
                ToolCallRecord(
                    tool="sales_sql",
                    output_preview='{"product_id":8,"revenue":138612.78}',
                )
            ],
            confidence=0.9,
        )
        evaluator = Evaluator(SimpleNamespace(settings=SimpleNamespace()))

        _, hard_ok, _, issues = evaluator._rules(task, result)

        assert not hard_ok
        assert any("not present in successful tool outputs" in issue for issue in issues)

    def test_analyst_accepts_values_supported_by_tool_output(self):
        from types import SimpleNamespace

        from app.orchestrator.evaluator import Evaluator
        from app.schemas.workflow import AgentResult, Finding, TaskSpec, ToolCallRecord

        task = TaskSpec(
            id="task-1",
            title="Analyze sales",
            description="Quantify product performance.",
            agent="analyst",
        )
        result = AgentResult(
            task_id="task-1",
            agent="analyst",
            summary="The sales figures show the product revenue and margin.",
            findings=[
                Finding(
                    statement="Revenue was approximately $138,613.",
                    evidence="Query result reported $138,612.78 in revenue.",
                    source="sales_sql",
                ),
                Finding(
                    statement="Gross margin was approximately 55%.",
                    evidence="Query result reported a margin of 55.12%.",
                    source="sales_sql",
                ),
            ],
            tool_calls=[
                ToolCallRecord(
                    tool="sales_sql",
                    output_preview='{"revenue":138612.78,"margin_pct":55.12}',
                )
            ],
            confidence=0.9,
        )
        evaluator = Evaluator(SimpleNamespace(settings=SimpleNamespace()))

        _, hard_ok, _, issues = evaluator._rules(task, result)

        assert hard_ok
        assert not any("quantified claims" in issue for issue in issues)

    def test_partial_report_excludes_rejected_task_findings(self):
        from types import SimpleNamespace

        from app.orchestrator.synthesizer import Synthesizer

        runtime = SimpleNamespace(
            settings=SimpleNamespace(),
            run_id="run-grounding",
            token_usage=lambda: {"calls": 0, "total_tokens": 0},
            llm=SimpleNamespace(exhausted=False),
        )
        state = {
            "objective": "Analyze product performance",
            "plan": {
                "tasks": [
                    {"id": "task-1", "title": "Analyze sales", "agent": "analyst"}
                ]
            },
            "results": {
                "task-1": {
                    "status": "completed",
                    "summary": "Fabricated result with $12,340 revenue.",
                    "findings": [
                        {
                            "statement": "Revenue was $12,340.",
                            "evidence": "Unverified",
                            "source": "sales_sql",
                        }
                    ],
                    "confidence": 0.9,
                }
            },
            "evaluations": {"task-1": {"passed": False, "issues": ["unsupported value"]}},
        }

        report, source = Synthesizer(runtime)._compose(state)

        assert source == "template"
        assert "$12,340" not in report
        assert "Only 0 of 1 planned tasks passed evaluation" in report

    def test_synthesizer_rejects_ungrounded_llm_numbers(self):
        from types import SimpleNamespace

        from app.llm.base import LLMResponse
        from app.orchestrator.synthesizer import Synthesizer

        class FakeLLM:
            exhausted = False

            def invoke(self, *args, **kwargs):
                return LLMResponse(
                    content=(
                        "# Report\n\n"
                        "The measured revenue was $12,340, and this analysis is complete. "
                        + "Additional details. " * 45
                    ),
                    model="fake",
                    provider="fake",
                )

        runtime = SimpleNamespace(
            settings=SimpleNamespace(),
            run_id="run-grounding",
            token_usage=lambda: {"calls": 1, "total_tokens": 10},
            llm=FakeLLM(),
        )
        state = {
            "objective": "Analyze product performance",
            "plan": {
                "tasks": [
                    {"id": "task-1", "title": "Analyze sales", "agent": "analyst"}
                ]
            },
            "results": {
                "task-1": {
                    "status": "completed",
                    "summary": "Sales SQL returned revenue of approximately $138,613.",
                    "findings": [
                        {
                            "statement": "Revenue was approximately $138,613.",
                            "evidence": "sales_sql returned revenue 138612.78.",
                            "source": "sales_sql",
                        }
                    ],
                    "tool_calls": [
                        {
                            "tool": "sales_sql",
                            "ok": True,
                            "output_preview": '{"revenue":138612.78}',
                        }
                    ],
                    "confidence": 0.9,
                }
            },
            "evaluations": {"task-1": {"passed": True, "score": 1.0}},
        }

        report, source = Synthesizer(runtime)._compose(state)

        assert source == "template"
        assert "$12,340" not in report


# ===========================================================================
# Structured logging (foundation)
# ===========================================================================
class TestLogging:
    def test_json_formatter_emits_valid_json_with_extras(self):
        from app.core.logging import JsonFormatter

        record = logging.LogRecord(
            name="agentforge.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        record.run_id = "run-123"
        payload = json.loads(JsonFormatter().format(record))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "agentforge.test"
        assert payload["message"] == "hello world"
        assert payload["run_id"] == "run-123"
        assert "ts" in payload

    def test_human_formatter_contains_message_and_extras(self):
        from app.core.logging import HumanFormatter

        record = logging.LogRecord(
            name="agentforge.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="careful",
            args=(),
            exc_info=None,
        )
        record.run_id = "run-42"
        text = HumanFormatter().format(record)
        assert "careful" in text and "run_id=run-42" in text

    def test_reserved_extra_keys_raise_keyerror(self):
        """Documented stdlib caveat: ``extra`` cannot shadow LogRecord attributes."""
        logger = logging.getLogger("agentforge.test.reserved")
        logger.setLevel(logging.INFO)
        with pytest.raises(KeyError):
            logger.warning("message", extra={"name": "trying-to-shadow"})

    def test_setup_logging_is_idempotent(self):
        from app.core import logging as app_logging

        root = logging.getLogger()
        original_handlers = list(root.handlers)
        original_level = root.level
        try:
            app_logging.setup_logging(level="WARNING", json_logs=False)
            first = len(root.handlers)
            app_logging.setup_logging(level="WARNING", json_logs=False)
            assert len(root.handlers) == first == 1
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in original_handlers:
                root.addHandler(handler)
            root.setLevel(original_level)
