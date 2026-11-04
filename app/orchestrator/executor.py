"""Execute exactly one task with one agent: timeout, observability, persistence.

Agents run in a short-lived worker thread so a slow provider or tool cannot hang the
whole graph forever; on timeout the task fails gracefully and the workflow moves on.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from app.agents import get_agent
from app.agents.base import AgentContext, BaseAgent
from app.orchestrator import persistence
from app.orchestrator.runtime import RunRuntime
from app.schemas.workflow import AgentResult, AgentRole, TaskSpec, TaskStatus

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, runtime: RunRuntime) -> None:
        self.runtime = runtime
        self.settings = runtime.settings

    # -- public -----------------------------------------------------------
    def execute(
        self,
        task: TaskSpec,
        *,
        attempt: int,
        prior_results: dict[str, dict[str, Any]],
        approval_granted: bool,
        feedback: str = "",
    ) -> AgentResult:
        agent = get_agent(task.agent)
        context = self.build_context(
            task,
            agent,
            attempt=attempt,
            prior_results=prior_results,
            approval_granted=approval_granted,
            feedback=feedback,
        )
        timeout = max(30, int(self.settings.step_timeout_seconds))
        started = time.perf_counter()
        result = self._run_with_timeout(agent, task, context, timeout)
        if result is None:
            result = AgentResult(
                task_id=task.id,
                agent=task.agent,
                status=TaskStatus.FAILED,
                summary="",
                error=f"agent execution timed out after {timeout}s",
                metrics={"timeout": True, "attempt": attempt},
            )
        if self.runtime.is_cancelled() and result.status != TaskStatus.FAILED:
            result.status = TaskStatus.FAILED
            result.error = "cancelled"
        latency_ms = result.metrics.get("latency_ms") or int((time.perf_counter() - started) * 1000)
        result.metrics["latency_ms"] = latency_ms

        persistence.upsert_task(
            self.runtime.run_id,
            task.id,
            title=task.title,
            agent=task.agent.value,
            status="completed" if result.status == TaskStatus.COMPLETED else "failed",
            attempts=attempt,
            result=result.model_dump(mode="json"),
        )
        self._persist_artifacts(result)
        self.runtime.emit(
            "agent.completed",
            agent=agent.name,
            task_id=task.id,
            message=f"{agent.name} agent completed task {task.id} ({result.status.value})",
            data={
                "status": result.status.value,
                "summary": (result.summary or "")[:600],
                "confidence": result.confidence,
                "findings_count": len(result.findings),
                "tool_calls": [
                    {"tool": call.tool, "ok": call.ok, "duration_ms": call.duration_ms}
                    for call in result.tool_calls
                ],
                "artifacts": [artifact.name for artifact in result.artifacts],
                "attempt": attempt,
                "error": result.error,
                "latency_ms": latency_ms,
            },
            duration_ms=latency_ms,
        )
        return result

    # -- artifacts --------------------------------------------------------
    def _persist_artifacts(self, result: AgentResult) -> None:
        """Persist agent-produced artifact files as ``artifacts`` rows (deduped)."""
        for artifact in result.artifacts[:6]:
            try:
                content: str | None = None
                if artifact.path:
                    path = Path(artifact.path)
                    if path.is_file() and path.stat().st_size <= 2_000_000:
                        content = path.read_text(encoding="utf-8", errors="replace")
                persistence.ensure_artifact(
                    self.runtime.run_id,
                    name=artifact.name or Path(artifact.path).name or "artifact",
                    description=artifact.description
                    or f"Artifact produced by the {result.agent.value} agent (task {result.task_id}).",
                    kind=artifact.kind or "markdown",
                    path=artifact.path,
                    content=content,
                )
            except Exception:  # pragma: no cover - artifact rows are non-critical
                logger.exception(
                    "agent_artifact_persist_failed",
                    extra={"run_id": self.runtime.run_id, "task_id": result.task_id},
                )

    # -- context ----------------------------------------------------------
    def build_context(
        self,
        task: TaskSpec,
        agent: BaseAgent,
        *,
        attempt: int,
        prior_results: dict[str, dict[str, Any]],
        approval_granted: bool,
        feedback: str,
    ) -> AgentContext:
        tool_context = self.runtime.make_tool_context(
            task.id,
            agent.name,
            approval_granted=approval_granted,
            attempt=attempt,
        )
        allowed_scopes = set(agent.scopes)
        if agent.role == AgentRole.EXECUTION:
            # The execution agent may use approved action tools; scopes are granted
            # only for tools that are actually registered and gated by approval.
            try:
                for spec in self.runtime.registry.list_specs():
                    if spec.sensitive:
                        allowed_scopes.update(spec.scopes)
            except Exception:  # pragma: no cover - registry discovery defensive
                logger.warning("execution_scope_discovery_failed", extra={"run_id": self.runtime.run_id})
        selected_prior = self._prior_results_for(task, prior_results)
        return AgentContext(
            run_id=self.runtime.run_id,
            task_id=task.id,
            agent=agent.name,
            settings=self.settings,
            llm=self.runtime.llm,
            registry=self.runtime.registry,
            tool_context=tool_context,
            allowed_scopes=tuple(sorted(allowed_scopes)),
            attempt=attempt,
            feedback=feedback,
            prior_results=selected_prior,
            approval_granted=approval_granted,
            artifacts_dir=self.settings.artifacts_path,
            max_tool_steps=agent.max_tool_steps,
            step_max_tokens=max(1800, agent.step_max_tokens),
            on_event=self._event_sink(agent.name, task.id),
            is_cancelled=self.runtime.is_cancelled,
        )

    def _event_sink(self, agent_name: str, task_id: str):
        def sink(event_type: str, data: dict[str, Any]) -> None:
            duration_ms = data.get("duration_ms") if event_type == "tool.called" else None
            self.runtime.emit(
                event_type,
                agent=str(data.get("agent") or agent_name),
                task_id=task_id,
                message=str(data.get("message") or ""),
                data={key: value for key, value in data.items() if key not in {"message", "agent"}},
                duration_ms=duration_ms if isinstance(duration_ms, int) else None,
            )

        return sink

    def _prior_results_for(
        self,
        task: TaskSpec,
        prior_results: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for dep in task.depends_on:
            if dep in prior_results:
                selected[dep] = prior_results[dep]
        # Include recent completed results so reporting/execution agents see context.
        for task_id, result in prior_results.items():
            if task_id in selected:
                continue
            if isinstance(result, dict) and result.get("status") == "completed":
                selected[task_id] = result
        return selected

    # -- timeout ----------------------------------------------------------
    def _run_with_timeout(
        self,
        agent: BaseAgent,
        task: TaskSpec,
        context: AgentContext,
        timeout: int,
    ) -> AgentResult | None:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"agent-{agent.name}")
        future = pool.submit(agent.execute, task, context)
        try:
            result = future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            logger.warning(
                "agent_timeout",
                extra={"run_id": self.runtime.run_id, "task_id": task.id, "timeout": timeout},
            )
            result = None
        except Exception as exc:  # pragma: no cover - defensive (agent swallows its own errors)
            logger.exception("agent_execution_crashed", extra={"task_id": task.id})
            result = AgentResult(
                task_id=task.id,
                agent=task.agent,
                status=TaskStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            # Never block the workflow waiting for a hung thread; it is bounded by the
            # provider timeout and will be reaped by the interpreter.
            pool.shutdown(wait=False, cancel_futures=True)
        return result


__all__ = ["Executor"]
