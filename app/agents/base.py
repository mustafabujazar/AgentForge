"""Agent base class: bounded ReAct-style tool loop with structured JSON output.

Design notes
------------
* One ``execute(task, ctx)`` call is *one attempt*.  The executor owns retries,
  timeouts and event emission; the agent owns the reasoning loop.
* The loop is strictly bounded: at most ``ctx.max_tool_steps`` (default 3) tool
  steps, one JSON repair call on parse failure, then one final synthesis call over
  the observations.  The run LLM budget is enforced by ``RunLLM``.
* All tool access goes through the shared :class:`~app.tools.registry.ToolRegistry`
  with the agent's own allow-list + scopes, so governance is enforced even when the
  model hallucinates a tool name.
* Failures never raise: the agent returns ``AgentResult(status=failed, error=...)``
  with whatever observations it collected, and the workflow keeps going.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from app.llm.base import ChatMessage, extract_json
from app.schemas.workflow import (
    AgentResult,
    AgentRole,
    ArtifactRef,
    Finding,
    TaskSpec,
    TaskStatus,
    ToolCallRecord,
)
from app.tools.base import BaseTool, ToolContext, ToolResult
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_OBSERVATION_CHARS = 2500
MAX_HISTORY_ITEM_CHARS = 1600


def slugify(value: str, *, max_len: int = 48) -> str:
    """Filesystem-safe slug used for artifact names."""
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "report")[:max_len].strip("-") or "report"


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(low, min(high, number))


def _truncate(text: Any, limit: int) -> str:
    raw = text if isinstance(text, str) else json.dumps(text, default=str)
    if len(raw) <= limit:
        return raw
    return raw[:limit] + f"... [truncated, {len(raw)} chars total]"


@dataclass
class AgentContext:
    """Execution-scoped object supplied by the executor.

    Carries everything an agent is allowed to touch: the (budgeted) LLM, the tool
    registry + tool context, prior results, approval state, cancellation signal and
    an event sink for observability.  Agents never import the orchestrator.
    """

    run_id: str
    task_id: str
    agent: str
    settings: Any
    llm: Any  # RunLLM facade; exposes .invoke/.remaining and is budget-enforced
    registry: ToolRegistry
    tool_context: ToolContext
    allowed_scopes: tuple[str, ...] = ()
    attempt: int = 1
    feedback: str = ""
    prior_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    approval_granted: bool = False
    artifacts_dir: Path | None = None
    max_tool_steps: int = 3
    step_max_tokens: int = 1800
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    is_cancelled: Callable[[], bool] | None = None
    last_llm_error: str | None = None

    # -- helpers ----------------------------------------------------------
    def emit(self, event_type: str, data: dict[str, Any], *, message: str = "") -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event_type, {**data, "message": message, "agent": self.agent})
        except Exception:  # pragma: no cover - observability must not break the agent
            logger.exception("agent_event_sink_failed")

    def cancelled(self) -> bool:
        try:
            return bool(self.is_cancelled and self.is_cancelled())
        except Exception:  # pragma: no cover
            return False

    @property
    def resolved_artifacts_dir(self) -> Path:
        path = self.artifacts_dir or self.settings.artifacts_path
        path.mkdir(parents=True, exist_ok=True)
        return path


_RESPONSE_FORMAT = """Reply with EXACTLY ONE complete, minified JSON object and nothing else (no prose, no markdown fences).
Reasoning models: keep your hidden reasoning brief and make sure the JSON is fully closed before the token limit.
{"thought": "<one short sentence>", "action": "tool", "tool": "<tool name>", "args": {...}}
or
{"thought": "<one short sentence>", "action": "final", "result": {"summary": "<what you concluded>", "findings": [{"statement": "...", "evidence": "...", "source": "...", "confidence": 0.0}], "confidence": 0.0, "artifact_markdown": "<optional full markdown document>", "metrics": {}}}"""


class BaseAgent:
    """Base class for the four specialized agents."""

    role: AgentRole = AgentRole.ANALYST
    name: str = "agent"
    description: str = ""
    capabilities: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    max_tool_steps: int = 3
    step_max_tokens: int = 1800
    temperature: float = 0.2

    # -- public API -------------------------------------------------------
    def execute(self, task: TaskSpec, ctx: AgentContext) -> AgentResult:
        started = time.perf_counter()
        ctx.max_tool_steps = min(
            max(1, int(getattr(ctx, "max_tool_steps", self.max_tool_steps))),
            self.max_tool_steps,
        )
        available = self.available_tools(ctx)
        tool_records: list[ToolCallRecord] = []
        produced_artifacts: list[ArtifactRef] = []
        observations: list[str] = []
        messages: list[ChatMessage] = [
            ChatMessage("system", self.build_system_prompt(task, ctx, available)),
            ChatMessage("user", "Begin. Return ONLY the JSON object for your next step."),
        ]
        final_payload: dict[str, Any] | None = None
        last_error: str | None = None
        tool_steps = 0

        try:
            for _ in range(ctx.max_tool_steps + 2):
                if ctx.cancelled():
                    last_error = "cancelled"
                    break
                response = self._call_llm(messages, ctx)
                if response is None:
                    last_error = last_error or ctx.last_llm_error or "LLM call failed"
                    break
                data = self._parse_response(response)
                if data is None:
                    repaired = self._repair(messages, response, ctx)
                    if repaired is not None:
                        data = self._parse_response(repaired)
                    if data is None:
                        last_error = "model did not return parseable JSON"
                        break

                action = str(data.get("action") or "").strip().lower()
                if not action:
                    if "tool" in data:
                        action = "tool"
                    elif "result" in data or "summary" in data:
                        action = "final"

                if action == "final":
                    final_payload = data.get("result")
                    if not isinstance(final_payload, dict):
                        final_payload = {
                            k: data.get(k)
                            for k in ("summary", "findings", "confidence", "artifact_markdown", "metrics")
                            if k in data
                        }
                    break

                if action == "tool":
                    tool_steps += 1
                    if tool_steps > ctx.max_tool_steps:
                        messages.append(
                            ChatMessage(
                                "user",
                                "Tool step budget exhausted. Reply now with action=final and your result JSON.",
                            )
                        )
                        continue
                    tool_name = str(data.get("tool") or "").strip()
                    args = data.get("args") if isinstance(data.get("args"), dict) else {}
                    record, observation, artifact = self._run_tool(tool_name, args, ctx, available)
                    tool_records.append(record)
                    if artifact is not None:
                        produced_artifacts.append(artifact)
                    observations.append(observation)
                    messages.append(
                        ChatMessage(
                            "assistant",
                            _truncate(json.dumps(data, default=str), MAX_HISTORY_ITEM_CHARS),
                        )
                    )
                    messages.append(ChatMessage("user", observation))
                    continue

                # Unknown action → nudge the model once, then count as a step.
                tool_steps += 1
                messages.append(
                    ChatMessage(
                        "user",
                        "Invalid action. Use action='tool' (with tool + args) or "
                        "action='final' (with result). Return one JSON object only.",
                    )
                )

            if final_payload is None and observations and ctx.cancelled() is False:
                final_payload = self._final_synthesis(messages, ctx, task)

        except Exception as exc:  # pragma: no cover - defensive: agents never crash a run
            logger.exception("agent_execute_failed", extra={"agent": self.role.value, "run_id": ctx.run_id})
            last_error = f"{type(exc).__name__}: {exc}"

        result = self._build_result(
            task=task,
            ctx=ctx,
            final_payload=final_payload,
            tool_records=tool_records,
            observations=observations,
            produced_artifacts=produced_artifacts,
            last_error=last_error,
            started=started,
        )
        return result

    # -- tools ------------------------------------------------------------
    def resolve_tool_names(self, ctx: AgentContext) -> tuple[str, ...]:
        return self.allowed_tools

    def available_tools(self, ctx: AgentContext) -> list[BaseTool]:
        tools: list[BaseTool] = []
        for name in self.resolve_tool_names(ctx):
            try:
                tool = ctx.registry.get(name)
            except Exception:  # registry discovery must not break an attempt
                tool = None
            if tool is None:
                continue
            # Sensitive tools are only exposed when the run carries an approval.
            if tool.spec.sensitive and not ctx.approval_granted:
                continue
            tools.append(tool)
        return tools

    def _run_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: AgentContext,
        available: list[BaseTool],
    ) -> tuple[ToolCallRecord, str, ArtifactRef | None]:
        allowed = {tool.spec.name for tool in available}
        if not tool_name:
            return (*self._rejected_tool("", args, ctx, "no tool name provided", allowed), None)
        if tool_name not in allowed:
            return (
                *self._rejected_tool(
                    tool_name,
                    args,
                    ctx,
                    f"tool '{tool_name}' is not available to the {self.role.value} agent; "
                    f"available: {sorted(allowed)}",
                    allowed,
                ),
                None,
            )
        started = time.perf_counter()
        result: ToolResult
        try:
            result = ctx.registry.execute(
                tool_name,
                args,
                ctx.tool_context,
                allowed_scopes=ctx.allowed_scopes or None,
            )
        except Exception as exc:  # pragma: no cover - registry normally swallows tool errors
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")
        duration_ms = result.duration_ms or int((time.perf_counter() - started) * 1000)
        preview = result.preview(MAX_OBSERVATION_CHARS) if result.ok else ""
        record = ToolCallRecord(
            tool=tool_name,
            args=_safe_args(args),
            ok=bool(result.ok),
            output_preview=(preview or "")[:MAX_OBSERVATION_CHARS],
            duration_ms=duration_ms,
            error=None if result.ok else (result.error or "tool failed"),
        )
        ctx.emit(
            "tool.called",
            {
                "tool": tool_name,
                "args_preview": _truncate(json.dumps(_safe_args(args), default=str), 300),
                "ok": record.ok,
                "duration_ms": duration_ms,
                "output_preview": (preview or "")[:600],
                "error": record.error,
            },
        )
        observation = (
            f"Observation from tool '{tool_name}' (ok={record.ok}, {duration_ms}ms):\n"
            + (preview if result.ok else f"ERROR: {record.error}")
        )
        artifact = self._artifact_from_tool_result(tool_name, result)
        return record, _truncate(observation, MAX_OBSERVATION_CHARS), artifact

    def _artifact_from_tool_result(
        self, tool_name: str, result: ToolResult
    ) -> ArtifactRef | None:
        """Convert a successful artifact-writing tool result into an ``ArtifactRef``."""
        if not result.ok or not isinstance(result.output, dict):
            return None
        path = result.output.get("path")
        if not path:
            return None
        try:
            return ArtifactRef(
                name=str(result.output.get("name") or Path(str(path)).name)[:255],
                kind=str(result.output.get("kind") or "markdown")[:32],
                path=str(path)[:1024],
                description=str(result.output.get("description") or f"Written by {tool_name}")[:1000],
            )
        except ValidationError:  # pragma: no cover - defensive
            return None

    def _rejected_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: AgentContext,
        error: str,
        allowed: set[str],
    ) -> tuple[ToolCallRecord, str]:
        record = ToolCallRecord(
            tool=tool_name or "<missing>",
            args=_safe_args(args),
            ok=False,
            output_preview="",
            duration_ms=0,
            error=error,
        )
        ctx.emit(
            "tool.called",
            {"tool": record.tool, "args_preview": "", "ok": False, "duration_ms": 0, "error": error},
        )
        return record, f"Observation: tool call rejected — {error}"

    # -- LLM helpers ------------------------------------------------------
    def _call_llm(self, messages: list[ChatMessage], ctx: AgentContext):
        """Call the budgeted LLM, repairing truncated JSON once.

        Reasoning models can spend most of ``max_tokens`` on hidden reasoning and
        truncate the JSON, which strict JSON-mode providers reject with HTTP 400.
        A single repair retry with a stricter reminder (and a slightly larger budget)
        recovers most of those cases without failing the attempt.
        """
        attempt = 0
        while attempt < 2:
            try:
                return ctx.llm.invoke(
                    messages,
                    temperature=self.temperature,
                    max_tokens=ctx.step_max_tokens + (600 if attempt else 0),
                    json_mode=True,
                    agent=self.role.value,
                )
            except Exception as exc:
                text = str(exc)
                truncated = "Failed to validate JSON" in text or "HTTP 400" in text
                if attempt == 0 and truncated:
                    logger.warning(
                        "agent_llm_json_rejected_retrying",
                        extra={
                            "agent": self.role.value,
                            "run_id": ctx.run_id,
                            "error": text[:400],
                        },
                    )
                    messages = [
                        *messages,
                        ChatMessage(
                            "user",
                            "IMPORTANT: your previous completion was rejected because the JSON "
                            "was invalid or truncated by the token limit. Reply now with ONE "
                            "complete minified JSON object and nothing else; keep 'thought' under "
                            "15 words and close every brace before finishing.",
                        ),
                    ]
                    attempt += 1
                    continue
                error_text = f"{type(exc).__name__}: {text}"[:500]
                ctx.last_llm_error = error_text
                logger.warning(
                    "agent_llm_call_failed",
                    extra={"agent": self.role.value, "run_id": ctx.run_id, "error": error_text},
                )
                return None
        return None

    def _parse_response(self, response: Any) -> dict[str, Any] | None:
        content = getattr(response, "content", "") or ""
        try:
            data = extract_json(content)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _repair(self, messages: list[ChatMessage], response: Any, ctx: AgentContext):
        messages.append(ChatMessage("assistant", _truncate(getattr(response, "content", ""), 2000)))
        messages.append(
            ChatMessage(
                "user",
                "That was not one valid JSON object. Reply again with ONLY the JSON object "
                "matching the required format — no markdown fences, no prose.",
            )
        )
        return self._call_llm(messages, ctx)

    def _final_synthesis(
        self,
        messages: list[ChatMessage],
        ctx: AgentContext,
        task: TaskSpec,
    ) -> dict[str, Any] | None:
        """One last call forcing a final JSON answer from the collected observations."""
        messages.append(
            ChatMessage(
                "user",
                "You have no more tool steps. Using ONLY the observations above, reply now with "
                "action='final' and the complete result JSON for the task.",
            )
        )
        response = self._call_llm(messages, ctx)
        if response is None:
            return None
        data = self._parse_response(response)
        if not data:
            return None
        if str(data.get("action") or "").lower() == "final":
            payload = data.get("result")
            if isinstance(payload, dict):
                return payload
        if isinstance(data.get("result"), dict):
            return data["result"]
        if "summary" in data or "findings" in data:
            return data
        return None

    # -- prompt -----------------------------------------------------------
    def tool_catalog_text(self, tools: list[BaseTool]) -> str:
        """Compact tool catalog (keeps prompts small; full schemas are registry-owned)."""
        if not tools:
            return "(no tools available for this agent)"
        lines: list[str] = []
        for tool in tools:
            try:
                spec = tool.spec
                schema = spec.input_schema or {}
                properties = schema.get("properties") or {}
                arg_types = {
                    name: (value.get("type") if isinstance(value, dict) else "string")
                    for name, value in properties.items()
                }
                required = schema.get("required") or []
                lines.append(
                    f"- {spec.name}: {spec.description[:220]} | "
                    f"args={json.dumps(arg_types, default=str)} required={required}"
                )
            except Exception:  # pragma: no cover
                continue
        return "\n".join(lines)

    def prior_results_text(self, ctx: AgentContext) -> str:
        if not ctx.prior_results:
            return "(none yet)"
        chunks: list[str] = []
        for task_id, result in list(ctx.prior_results.items())[:6]:
            if not isinstance(result, dict):
                continue
            status = result.get("status", "")
            summary = _truncate(result.get("summary", ""), 350)
            findings = result.get("findings") or []
            statements = "; ".join(
                _truncate(f.get("statement", ""), 160)
                for f in findings[:3]
                if isinstance(f, dict)
            )
            chunks.append(
                f"- [{task_id}] status={status} summary={summary}"
                + (f" findings={statements}" if statements else "")
            )
        return "\n".join(chunks)

    def build_system_prompt(
        self,
        task: TaskSpec,
        ctx: AgentContext,
        tools: list[BaseTool],
    ) -> str:
        criteria = "; ".join(task.success_criteria) or "(infer from the task)"
        feedback = (
            f"\nRETRY FEEDBACK FROM EVALUATOR (address it): {_truncate(ctx.feedback, 700)}"
            if ctx.feedback
            else ""
        )
        approval = (
            "GRANTED — you may use approved sensitive action tools."
            if ctx.approval_granted
            else "NOT granted — do not attempt sensitive actions; plan/report only."
        )
        return (
            f"You are the {self.role.value.upper()} agent in AgentForge, an autonomous business "
            f"automation platform. You are one specialist in a governed multi-agent workflow.\n\n"
            f"ROLE: {self.description}\n"
            f"CAPABILITIES: {', '.join(self.capabilities) or self.role.value}\n"
            f"TASK: {task.title}\n"
            f"TASK DETAIL: {task.description}\n"
            f"EXPECTED OUTPUT: {task.expected_output or 'structured findings + summary'}\n"
            f"SUCCESS CRITERIA: {criteria}\n"
            f"ATTEMPT: {ctx.attempt} of at most 3.\n"
            f"APPROVAL: {approval}{feedback}\n\n"
            f"PRIOR TASK RESULTS (use them; do not redo their work):\n{self.prior_results_text(ctx)}\n\n"
            f"AVAILABLE TOOLS ({len(tools)}):\n{self.tool_catalog_text(tools)}\n\n"
            f"RULES:\n"
            f"- You may take at most {ctx.max_tool_steps} tool steps. Prefer tools over guessing.\n"
            f"- Ground every finding in tool evidence (numbers, memo quotes, URLs). No invented data.\n"
            f"- Keep findings terse; confidence is a number between 0 and 1.\n"
            f"- {self.role_instructions()}\n\n"
            f"{_RESPONSE_FORMAT}"
        )

    def role_instructions(self) -> str:
        return "Complete the task and return the final structured result."

    # -- result building --------------------------------------------------
    def _build_result(
        self,
        *,
        task: TaskSpec,
        ctx: AgentContext,
        final_payload: dict[str, Any] | None,
        tool_records: list[ToolCallRecord],
        observations: list[str],
        produced_artifacts: list[ArtifactRef] | None = None,
        last_error: str | None = None,
        started: float,
    ) -> AgentResult:
        latency_ms = int((time.perf_counter() - started) * 1000)
        if final_payload is not None:
            payload = dict(final_payload)
            raw_findings = payload.get("findings") or []
            findings: list[Finding] = []
            if isinstance(raw_findings, list):
                for item in raw_findings[:10]:
                    finding = self._coerce_finding(item)
                    if finding is not None:
                        findings.append(finding)
            summary = str(payload.get("summary") or "").strip()
            metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
            artifact_markdown = payload.get("artifact_markdown") or metrics.get("artifact_markdown")
            if artifact_markdown:
                metrics = {**metrics, "artifact_markdown": str(artifact_markdown)}
            artifacts = self._merge_artifacts(
                self._coerce_artifacts(payload.get("artifacts")), produced_artifacts or []
            )
            status = TaskStatus.COMPLETED
            error: str | None = None
            if not summary and not findings and not artifact_markdown:
                status = TaskStatus.FAILED
                error = "model returned a final result without summary/findings/artifact"
            elif ctx.cancelled():
                status = TaskStatus.FAILED
                error = "cancelled"
        else:
            summary, findings, metrics, artifacts = self._fallback_content(observations, tool_records)
            artifacts = self._merge_artifacts(artifacts, produced_artifacts or [])
            status = TaskStatus.FAILED
            error = last_error or "agent did not produce a final structured result"
            metrics = {**metrics, "degraded": True}

        metrics = {
            **metrics,
            "tool_calls": len(tool_records),
            "tool_ok": sum(1 for r in tool_records if r.ok),
            "observations": len(observations),
            "latency_ms": latency_ms,
            "attempt": ctx.attempt,
        }
        if ctx.last_llm_error:
            metrics["llm_error"] = ctx.last_llm_error
        try:
            confidence = _clamp((final_payload or {}).get("confidence", 0.0))
        except Exception:  # pragma: no cover - defensive
            confidence = 0.0
        result = AgentResult(
            task_id=task.id,
            agent=self.role,
            status=status,
            summary=summary,
            findings=findings,
            artifacts=artifacts,
            tool_calls=tool_records,
            confidence=confidence,
            metrics=_json_safe(metrics),
            error=error,
        )
        if result.confidence == 0.0 and findings:
            result.confidence = round(
                sum(f.confidence for f in findings) / max(1, len(findings)), 3
            )
        return self.postprocess(result, ctx, task)

    def _merge_artifacts(
        self, first: list[ArtifactRef], second: list[ArtifactRef]
    ) -> list[ArtifactRef]:
        merged: list[ArtifactRef] = []
        seen: set[str] = set()
        for artifact in [*first, *second]:
            key = artifact.path or artifact.name
            if key in seen:
                continue
            seen.add(key)
            merged.append(artifact)
        return merged[:6]

    def _coerce_finding(self, item: Any) -> Finding | None:
        if isinstance(item, str):
            item = {"statement": item}
        if not isinstance(item, dict):
            return None
        statement = str(item.get("statement") or item.get("claim") or "").strip()
        if not statement:
            return None
        try:
            return Finding(
                statement=statement[:1200],
                evidence=str(item.get("evidence") or "")[:2000],
                source=str(item.get("source") or "")[:300],
                confidence=_clamp(item.get("confidence", 0.5)),
            )
        except ValidationError:  # pragma: no cover - defensive
            return None

    def _coerce_artifacts(self, raw: Any) -> list[ArtifactRef]:
        artifacts: list[ArtifactRef] = []
        if not isinstance(raw, list):
            return artifacts
        for item in raw[:5]:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                continue
            try:
                artifacts.append(
                    ArtifactRef(
                        name=str(item.get("name") or "artifact")[:255],
                        kind=str(item.get("kind") or "markdown")[:32],
                        path=str(item.get("path") or "")[:1024],
                        description=str(item.get("description") or "")[:1000],
                    )
                )
            except ValidationError:  # pragma: no cover
                continue
        return artifacts

    def _fallback_content(
        self,
        observations: list[str],
        tool_records: list[ToolCallRecord],
    ) -> tuple[str, list[Finding], dict[str, Any], list[ArtifactRef]]:
        ok_calls = [record for record in tool_records if record.ok]
        findings: list[Finding] = []
        for record in ok_calls[:3]:
            findings.append(
                Finding(
                    statement=f"Tool '{record.tool}' returned data relevant to the task.",
                    evidence=record.output_preview[:500],
                    source=f"tool:{record.tool}",
                    confidence=0.35,
                )
            )
        if ok_calls:
            summary = (
                f"Executed {len(tool_records)} tool call(s) ({len(ok_calls)} successful) but did "
                f"not receive a structured final answer from the model; partial evidence retained."
            )
        else:
            summary = ""
        return summary, findings, {"degraded": True}, []

    # -- extension hook ---------------------------------------------------
    def postprocess(self, result: AgentResult, ctx: AgentContext, task: TaskSpec) -> AgentResult:
        """Subclasses may persist artifacts or enrich metrics.  Must not raise."""
        return result

    # -- artifact helper --------------------------------------------------
    def write_artifact(
        self,
        ctx: AgentContext,
        task: TaskSpec,
        markdown: str,
        *,
        suffix: str,
        description: str,
    ) -> ArtifactRef | None:
        if not markdown.strip():
            return None
        try:
            filename = f"{slugify(task.id)}-{suffix}.md"
            path = ctx.resolved_artifacts_dir / filename
            path.write_text(markdown, encoding="utf-8")
            return ArtifactRef(
                name=filename,
                kind="markdown",
                path=str(path),
                description=description,
            )
        except Exception:  # pragma: no cover - artifact write failure is non-fatal
            logger.exception("agent_artifact_write_failed", extra={"run_id": ctx.run_id})
            return None


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(args, default=str))
    except (TypeError, ValueError):
        return {}


def _json_safe(value: Any) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {"repr": repr(value)}


__all__ = [
    "AgentContext",
    "BaseAgent",
    "MAX_OBSERVATION_CHARS",
    "slugify",
]
