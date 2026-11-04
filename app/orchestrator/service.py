"""Workflow service: frozen public interface for the API and scripts.

Public API (do not break — owned by api-db integration)::

    from app.orchestrator.service import workflow_service

    await workflow_service.start_run(run_id: str, objective: str)  # schedule, returns fast
    await workflow_service.resume_run(run_id: str)                 # after approval decision
    workflow_service.cancel(run_id: str)                           # cooperative cancel
    run_id = await workflow_service.run_sync(objective, user_id=None, auto_approve=False)

Execution model
---------------
The graph is synchronous (LangGraph sync ``invoke``).  ``start_run`` / ``resume_run``
therefore schedule a **daemon thread** per run and return immediately; the API event
loop is never blocked.  Events are published to :data:`app.core.events.broker` with
``publish_threadsafe`` and persisted to ``run_events`` by the emitter.

Each worker thread owns the run's DB writes (runs/task_records/evaluation_metrics/
artifacts/run_events) through fresh ``session_scope()`` sessions.

HITL pause/resume
-----------------
The sensitive-task approval gate uses LangGraph ``interrupt`` + ``MemorySaver``
keyed by ``thread_id=run_id``.  When the API updates the ``Approval`` row and calls
``resume_run``, the service invokes ``graph.invoke(Command(resume=...), config)``
which continues from the paused ``approval_wait`` node.  If the in-memory checkpoint
is gone (process restart) the service rebuilds the graph input from
``runs.state_json`` and re-enters the remaining task loop.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.core.config import Settings, get_settings
from app.core.errors import LLMConfigurationError
from app.llm.base import BaseLLM
from app.orchestrator import persistence
from app.orchestrator.graph import RECURSION_LIMIT, build_graph
from app.orchestrator.runtime import RunRuntime, WorkflowCancelled
from app.schemas.workflow import RunStatus
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
}


class WorkflowService:
    """Owns run lifecycle, background threads, the compiled graph and checkpoints."""

    def __init__(
        self,
        *,
        llm: BaseLLM | None = None,
        settings: Settings | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._llm_override = llm
        self._registry_override = registry
        self._lock = threading.RLock()
        self._runtimes: dict[str, RunRuntime] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()
        self._checkpointer = MemorySaver()
        self._graph = build_graph(self.get_runtime, checkpointer=self._checkpointer)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------
    async def start_run(self, run_id: str, objective: str) -> None:
        """Schedule workflow execution for an existing (queued) run row."""
        self._schedule_run(run_id, objective)

    async def resume_run(self, run_id: str) -> None:
        """Schedule resumption of a run paused at an approval gate."""
        self._schedule_resume(run_id)

    def cancel(self, run_id: str) -> None:
        """Request cooperative cancellation (sync, never raises)."""
        try:
            self._cancel(run_id)
        except Exception:  # pragma: no cover - cancel is best effort
            logger.exception("cancel_failed", extra={"run_id": run_id})

    async def run_sync(
        self,
        objective: str,
        user_id: str | None = None,
        auto_approve: bool = False,
    ) -> str:
        """Create + execute a run inline (blocking).  For scripts and tests."""
        run_id = _new_run_id()
        created = persistence.ensure_run(
            run_id, objective, user_id=user_id, auto_approve=auto_approve
        )
        runtime = self._build_runtime(run_id, objective, bool(auto_approve))
        if created:
            runtime.emit("run.created", message="Run created", data={"objective": objective[:500]})
        self._execute(run_id, runtime)
        return run_id

    # ------------------------------------------------------------------
    # Runtime registry (used by the graph's runtime provider)
    # ------------------------------------------------------------------
    def get_runtime(self, run_id: str) -> RunRuntime:
        with self._lock:
            runtime = self._runtimes.get(run_id)
        if runtime is None:
            raise KeyError(f"no active workflow runtime for run {run_id!r}")
        return runtime

    def has_runtime(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runtimes

    def _build_runtime(
        self,
        run_id: str,
        objective: str,
        auto_approve: bool,
        initial_usage: dict[str, Any] | None = None,
    ) -> RunRuntime:
        with self._lock:
            existing = self._runtimes.get(run_id)
            if existing is not None:
                return existing
            inner = self._llm_override if self._llm_override is not None else _default_llm()
            runtime = RunRuntime(
                run_id=run_id,
                objective=objective,
                auto_approve=auto_approve,
                settings=self._settings,
                llm_inner=inner,
                registry=self._registry_override,
                initial_usage=initial_usage or {},
                on_terminal=self._on_terminal,
            )
            self._runtimes[run_id] = runtime
            return runtime

    def _on_terminal(self, run_id: str) -> None:
        with self._lock:
            self._runtimes.pop(run_id, None)
            self._cancelled.discard(run_id)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _schedule_run(self, run_id: str, objective: str) -> None:
        created = persistence.ensure_run(run_id, objective)
        run = persistence.get_run(run_id)
        if run is None:  # pragma: no cover - ensure_run guarantees the row
            raise RuntimeError(f"run {run_id} could not be created")
        if run.status in TERMINAL_STATUSES:
            logger.warning("start_run_ignored_terminal", extra={"run_id": run_id, "status": run.status})
            return
        with self._lock:
            thread = self._threads.get(run_id)
            if thread is not None and thread.is_alive():
                logger.warning("start_run_already_running", extra={"run_id": run_id})
                return
            try:
                runtime = self._build_runtime(
                    run_id,
                    run.objective or objective,
                    bool(run.auto_approve),
                    initial_usage=dict(run.token_usage_json or {}),
                )
            except LLMConfigurationError as exc:
                logger.error("llm_configuration_error", extra={"run_id": run_id, "error": str(exc)})
                persistence.update_run(
                    run_id,
                    status="failed",
                    error=f"LLM configuration error: {exc}",
                    completed_at=datetime.now(timezone.utc),
                )
                return
            if created:
                logger.info("run_row_created_by_orchestrator", extra={"run_id": run_id})
            # The API layer delegates ALL run_events writing to the orchestrator, so
            # run.created is always emitted here (before run.started from intake).
            runtime.emit(
                "run.created",
                message="Run created",
                data={"objective": (run.objective or objective)[:500]},
            )
            thread = threading.Thread(
                target=self._thread_main,
                args=(run_id,),
                name=f"workflow-{run_id[:8]}",
                daemon=True,
            )
            self._threads[run_id] = thread
            thread.start()
        logger.info("workflow_scheduled", extra={"run_id": run_id})

    def _schedule_resume(self, run_id: str) -> None:
        approval_row = persistence.latest_approval(run_id)
        if approval_row is None:
            logger.warning("resume_no_approval", extra={"run_id": run_id})
            return
        if approval_row.status not in {"approved", "rejected"}:
            logger.warning(
                "resume_approval_not_decided",
                extra={"run_id": run_id, "status": approval_row.status},
            )
            return
        run = persistence.get_run(run_id)
        if run is None:
            logger.warning("resume_missing_run", extra={"run_id": run_id})
            return
        if run.status in TERMINAL_STATUSES:
            logger.warning("resume_run_terminal", extra={"run_id": run_id, "status": run.status})
            return
        with self._lock:
            thread = self._threads.get(run_id)
            if thread is not None and thread.is_alive():
                logger.warning("resume_already_running", extra={"run_id": run_id})
                return
            if run_id in self._cancelled:
                self._cancel_paused(run_id)
                return
            try:
                runtime = self._build_runtime(
                    run_id,
                    run.objective,
                    bool(run.auto_approve),
                    initial_usage=dict(run.token_usage_json or {}),
                )
            except LLMConfigurationError as exc:
                persistence.update_run(
                    run_id,
                    status="failed",
                    error=f"LLM configuration error: {exc}",
                    completed_at=datetime.now(timezone.utc),
                )
                return
            thread = threading.Thread(
                target=self._resume_main,
                args=(
                    run_id,
                    approval_row.id,
                    approval_row.status,
                    approval_row.task_id,
                    approval_row.note or "",
                ),
                name=f"workflow-resume-{run_id[:8]}",
                daemon=True,
            )
            self._threads[run_id] = thread
            thread.start()
        logger.info(
            "workflow_resume_scheduled",
            extra={"run_id": run_id, "decision": approval_row.status},
        )

    # ------------------------------------------------------------------
    # Worker entry points
    # ------------------------------------------------------------------
    def _thread_main(self, run_id: str) -> None:
        try:
            runtime = self.get_runtime(run_id)
            self._execute(run_id, runtime)
        except Exception:  # pragma: no cover - safety net
            logger.exception("workflow_thread_crashed", extra={"run_id": run_id})
            try:
                self.get_runtime(run_id).finalize_failed("internal workflow thread error")
            except Exception:
                persistence.update_run(
                    run_id, status="failed", error="internal workflow thread error"
                )
        finally:
            self._forget_thread(run_id)

    def _resume_main(
        self,
        run_id: str,
        approval_id: str,
        decision: str,
        task_id: str,
        note: str,
    ) -> None:
        try:
            runtime = self.get_runtime(run_id)
            runtime.cancel_event.clear()
            runtime.paused = False
            payload = {"decision": decision, "approval_id": approval_id, "task_id": task_id, "note": note}
            runtime.emit(
                "run.resumed",
                message=f"Run resumed after approval decision: {decision}",
                data=payload,
            )
            config = self._config(run_id)
            if self._has_checkpoint(run_id, config):
                result = self._graph.invoke(Command(resume=payload), config)
            else:
                logger.warning("resume_checkpoint_missing", extra={"run_id": run_id})
                state = self._fallback_resume_state(run_id, approval_id, decision, task_id)
                result = self._graph.invoke(state, config)
            self._handle_invoke_result(run_id, result, runtime)
        except WorkflowCancelled:
            logger.info("workflow_resume_cancelled", extra={"run_id": run_id})
            if self.has_runtime(run_id):
                self.get_runtime(run_id).mark_cancelled()
        except Exception as exc:
            logger.exception("workflow_resume_crashed", extra={"run_id": run_id})
            if self.has_runtime(run_id):
                self.get_runtime(run_id).finalize_failed(f"{type(exc).__name__}: {exc}")
            else:  # pragma: no cover - defensive
                persistence.update_run(run_id, status="failed", error=str(exc))
        finally:
            self._forget_thread(run_id)

    def _execute(self, run_id: str, runtime: RunRuntime) -> None:
        config = self._config(run_id)
        try:
            result = self._graph.invoke(self._initial_state(runtime), config)
            self._handle_invoke_result(run_id, result, runtime)
        except WorkflowCancelled:
            logger.info("workflow_cancelled", extra={"run_id": run_id})
            runtime.mark_cancelled()
        except Exception as exc:
            logger.exception("workflow_failed", extra={"run_id": run_id})
            runtime.finalize_failed(f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Graph result handling
    # ------------------------------------------------------------------
    def _handle_invoke_result(
        self,
        run_id: str,
        result: Any,
        runtime: RunRuntime,
    ) -> None:
        result = result if isinstance(result, dict) else {}
        if result.get("__interrupt__"):
            runtime.paused = True
            status, _ = persistence.run_status(run_id)
            if status not in {"awaiting_approval", "cancelled", "completed", "failed"}:
                persistence.update_run(
                    run_id,
                    status="awaiting_approval",
                    state_json=persistence.json_safe(result),
                    token_usage_json=runtime.token_usage(),
                )
            logger.info("workflow_paused_for_approval", extra={"run_id": run_id})
            return
        status, _ = persistence.run_status(run_id)
        if status in TERMINAL_STATUSES:
            runtime.finish()
            return
        if result.get("final_report"):
            runtime.finalize_completed(result)
        else:
            runtime.finalize_failed(
                str(result.get("error") or "workflow ended without producing a final report"),
                result,
            )

    def _initial_state(self, runtime: RunRuntime) -> dict[str, Any]:
        return {
            "run_id": runtime.run_id,
            "objective": runtime.objective,
            "auto_approve": runtime.auto_approve,
            "plan": {},
            "task_order": [],
            "current_index": 0,
            "current_task_id": None,
            "results": {},
            "evaluations": {},
            "attempts": {},
            "feedback": {},
            "approvals": {},
            "pending_approval_id": None,
            "awaiting_task_id": None,
            "final_report": None,
            "status": "queued",
            "error": None,
            "token_usage": {},
            "started_at": "",
            "completed_at": None,
            "context": {},
        }

    def _fallback_resume_state(
        self,
        run_id: str,
        approval_id: str,
        decision: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Rebuild graph input from ``runs.state_json`` when the checkpoint is gone."""
        state = persistence.load_state(run_id) or {}
        merged: dict[str, Any] = {**self._initial_state_from_db(run_id), **state}
        merged["run_id"] = run_id
        merged["status"] = "running"
        merged["error"] = None
        merged["pending_approval_id"] = None
        merged["awaiting_task_id"] = None
        merged["current_task_id"] = None
        approvals = dict(merged.get("approvals") or {})
        approvals[task_id] = decision
        merged["approvals"] = approvals
        context = dict(merged.get("context") or {})
        if decision == "rejected":
            skipped = dict(context.get("skipped") or {})
            skipped[task_id] = "approval rejected by human reviewer"
            context["skipped"] = skipped
        merged["context"] = context
        logger.info(
            "fallback_resume_state",
            extra={"run_id": run_id, "approval_id": approval_id, "decision": decision},
        )
        return merged

    def _initial_state_from_db(self, run_id: str) -> dict[str, Any]:
        run = persistence.get_run(run_id)
        return {
            "run_id": run_id,
            "objective": run.objective if run else "",
            "auto_approve": bool(run.auto_approve) if run else False,
            "plan": (dict(run.plan_json) if run and run.plan_json else {}),
            "task_order": [],
            "current_index": 0,
            "current_task_id": None,
            "results": {},
            "evaluations": {},
            "attempts": {},
            "feedback": {},
            "approvals": {},
            "pending_approval_id": None,
            "awaiting_task_id": None,
            "final_report": None,
            "status": "queued",
            "error": None,
            "token_usage": {},
            "started_at": "",
            "completed_at": None,
            "context": {},
        }

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------
    def _cancel(self, run_id: str) -> None:
        with self._lock:
            self._cancelled.add(run_id)
            runtime = self._runtimes.get(run_id)
            thread = self._threads.get(run_id)
        if runtime is not None:
            runtime.request_cancel()
        if thread is not None and thread.is_alive():
            logger.info("cancel_requested", extra={"run_id": run_id})
            return
        self._cancel_paused(run_id)

    def _cancel_paused(self, run_id: str) -> None:
        """Cancel a run that has no live worker thread (queued/awaiting approval)."""
        status, _ = persistence.run_status(run_id)
        if status in TERMINAL_STATUSES:
            return
        with self._lock:
            runtime = self._runtimes.get(run_id)
        if runtime is not None:
            runtime.request_cancel()
            runtime.mark_cancelled()
        else:
            persistence.update_run(
                run_id,
                status="cancelled",
                completed_at=datetime.now(timezone.utc),
            )
            from app.orchestrator.events import EventEmitter

            EventEmitter(run_id).emit("run.cancelled", message="Run cancelled")
        self._forget_thread(run_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _config(self, run_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "recursion_limit": RECURSION_LIMIT,
        }

    def _has_checkpoint(self, run_id: str, config: dict[str, Any]) -> bool:
        try:
            snapshot = self._graph.get_state(config)
        except Exception:  # pragma: no cover - defensive
            return False
        return bool(getattr(snapshot, "values", None)) and bool(getattr(snapshot, "next", None))

    def _forget_thread(self, run_id: str) -> None:
        current = threading.current_thread()
        with self._lock:
            thread = self._threads.get(run_id)
            if thread is None or thread is current:
                self._threads.pop(run_id, None)


def _default_llm() -> BaseLLM:
    """Resolve the configured provider lazily (never at import time)."""
    from app.llm.registry import get_llm

    return get_llm()


def _new_run_id() -> str:
    from app.db.models import new_id

    return new_id()


# Module-level singleton: imported lazily by the API (inside request handlers).
workflow_service = WorkflowService()


__all__ = ["WorkflowService", "workflow_service"]
