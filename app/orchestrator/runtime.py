"""Per-run execution runtime shared by the LangGraph nodes.

A ``RunRuntime`` holds everything whose lifetime spans multiple graph nodes (and,
for HITL pauses, multiple invocations): the budgeted LLM facade, the durable event
emitter, the cancellation flag, token accounting and the DB helpers needed to
snapshot state.  The service keeps one runtime per active (or paused) run keyed by
``run_id``; graph nodes look it up through :meth:`WorkflowService.get_runtime`.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.config import Settings
from app.db.base import get_session_factory
from app.orchestrator.events import EventEmitter
from app.orchestrator.llm_budget import RunLLM
from app.orchestrator.persistence import json_safe, update_run
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry, get_registry

logger = logging.getLogger(__name__)


class WorkflowCancelled(RuntimeError):
    """Raised inside a node/agent when cooperative cancellation is requested."""


class RunRuntime:
    def __init__(
        self,
        *,
        run_id: str,
        objective: str,
        auto_approve: bool,
        settings: Settings,
        llm_inner: Any,
        registry: ToolRegistry | None = None,
        initial_usage: dict[str, Any] | None = None,
        on_terminal: Callable[[str], None] | None = None,
    ) -> None:
        self.run_id = run_id
        self.objective = objective
        self.auto_approve = bool(auto_approve)
        self.settings = settings
        self.registry = registry or get_registry()
        self.emitter = EventEmitter(run_id)
        provider_name = str(getattr(settings, "llm_provider", "") or "").lower()
        pace_seconds = 0.0 if provider_name in {"mock", "fake"} else 1.5
        self.llm = RunLLM(
            llm_inner,
            max_calls=getattr(settings, "llm_max_calls_per_run", 40),
            on_call=self._on_llm_call,
            initial_usage=initial_usage or {},
            min_interval_seconds=pace_seconds,
        )
        self.cancel_event = threading.Event()
        self.started_at = time.monotonic()
        self.started_at_iso = datetime.now(timezone.utc).isoformat()
        self.llm_calls: list[dict[str, Any]] = []
        self.pending_approval_id: str | None = None
        self.paused = False
        self.terminal = False
        self._on_terminal = on_terminal
        self._lock = threading.Lock()

    # -- observability ----------------------------------------------------
    def _on_llm_call(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.llm_calls.append(payload)
            if len(self.llm_calls) > 200:
                self.llm_calls = self.llm_calls[-200:]
        logger.debug(
            "llm_call",
            extra={
                "run_id": self.run_id,
                "agent": payload.get("agent", ""),
                "call": payload.get("call"),
                "ok": payload.get("ok"),
                "latency_ms": payload.get("latency_ms"),
                "total_tokens": payload.get("total_tokens"),
            },
        )

    def emit(
        self,
        event_type: str,
        *,
        agent: str | None = None,
        task_id: str | None = None,
        message: str = "",
        data: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        return self.emitter.emit(
            event_type,
            agent=agent,
            task_id=task_id,
            message=message,
            data=data,
            duration_ms=duration_ms,
        )

    # -- cancellation -----------------------------------------------------
    def request_cancel(self) -> None:
        self.cancel_event.set()

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise WorkflowCancelled("run cancelled by user")

    # -- usage ------------------------------------------------------------
    def token_usage(self) -> dict[str, Any]:
        return self.llm.usage_dict()

    # -- tool context -----------------------------------------------------
    def make_tool_context(
        self,
        task_id: str,
        agent: str,
        *,
        approval_granted: bool = False,
        attempt: int = 1,
    ) -> ToolContext:
        return ToolContext(
            run_id=self.run_id,
            agent=agent,
            settings=self.settings,
            artifacts_dir=self.settings.artifacts_path,
            db_session_factory=get_session_factory(),
            extras={
                "task_id": task_id,
                "attempt": attempt,
                "approval_granted": bool(approval_granted),
                "auto_approve": self.auto_approve,
            },
        )

    # -- state snapshots --------------------------------------------------
    def snapshot(self, state: dict[str, Any], *, status: str | None = None) -> None:
        fields: dict[str, Any] = {
            "state_json": json_safe(state),
            "token_usage_json": self.token_usage(),
        }
        if status:
            fields["status"] = status
        try:
            update_run(self.run_id, **fields)
        except Exception:  # pragma: no cover - snapshots are best effort
            logger.exception("state_snapshot_failed", extra={"run_id": self.run_id})

    def save_plan(self, plan: Any) -> None:
        payload = plan.model_dump(mode="json") if hasattr(plan, "model_dump") else json_safe(plan)
        try:
            update_run(self.run_id, plan_json=payload)
        except Exception:  # pragma: no cover
            logger.exception("plan_save_failed", extra={"run_id": self.run_id})

    # -- lifecycle transitions -------------------------------------------
    def finish(self) -> None:
        with self._lock:
            if self.terminal:
                return
            self.terminal = True
        if self._on_terminal is not None:
            try:
                self._on_terminal(self.run_id)
            except Exception:  # pragma: no cover
                logger.exception("runtime_terminal_callback_failed", extra={"run_id": self.run_id})

    def finalize_completed(self, state: dict[str, Any]) -> None:
        context = state.get("context") or {}
        artifact = context.get("artifact") or {}
        report = state.get("final_report") or ""
        usage = self.token_usage()
        tasks = state.get("results") or {}
        completed = sum(1 for item in tasks.values() if (item or {}).get("status") == "completed")
        failed = sum(1 for item in tasks.values() if (item or {}).get("status") == "failed")
        try:
            update_run(
                self.run_id,
                status="completed",
                completed_at=datetime.now(timezone.utc),
                final_report=report,
                token_usage_json=usage,
                state_json=json_safe(state),
                error=None,
            )
        except Exception:  # pragma: no cover
            logger.exception("run_finalize_failed", extra={"run_id": self.run_id})
        self.emit(
            "run.completed",
            message="Run completed",
            data={
                "artifact_id": artifact.get("id"),
                "artifact_path": artifact.get("path"),
                "artifact_name": artifact.get("name"),
                "report_chars": len(report),
                "token_usage": usage,
                "tasks_completed": completed,
                "tasks_failed": failed,
                "duration_ms": int((time.monotonic() - self.started_at) * 1000),
            },
        )
        self.finish()

    def finalize_failed(self, error: str, state: dict[str, Any] | None = None) -> None:
        message = (error or "workflow failed")[:4000]
        try:
            update_run(
                self.run_id,
                status="failed",
                completed_at=datetime.now(timezone.utc),
                error=message,
                token_usage_json=self.token_usage(),
                state_json=json_safe(state) if state else None,
            )
        except Exception:  # pragma: no cover
            logger.exception("run_fail_write_failed", extra={"run_id": self.run_id})
        self.emit(
            "run.failed",
            message=message,
            data={"error": message, "token_usage": self.token_usage()},
        )
        self.finish()

    def mark_cancelled(self, state: dict[str, Any] | None = None) -> None:
        try:
            update_run(
                self.run_id,
                status="cancelled",
                completed_at=datetime.now(timezone.utc),
                token_usage_json=self.token_usage(),
                state_json=json_safe(state) if state else None,
            )
        except Exception:  # pragma: no cover
            logger.exception("run_cancel_write_failed", extra={"run_id": self.run_id})
        self.emit("run.cancelled", message="Run cancelled", data={"token_usage": self.token_usage()})
        self.finish()


__all__ = ["RunRuntime", "WorkflowCancelled"]
