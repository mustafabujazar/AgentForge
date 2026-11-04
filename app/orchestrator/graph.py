"""LangGraph workflow: intake → plan → task loop → (approval gate) → synthesize → finalize.

Graph shape
-----------
::

    START → intake → plan → dispatch ──────────────────────────────┐
                             ▲                                      │
                       advance │                                    │
                             │                                      │
                    execute → evaluate → retry (≤ MAX_RETRIES) ───┘
                       │            │
                       │            └→ (sensitive) approval_request → approval_wait(interrupt)
                       └→ synthesize → finalize → END

``dispatch`` only selects the next pending task; the sensitive-task approval gate
pauses the graph via ``interrupt`` (see ``approval.py``).  Every node snapshots the
JSON-serializable ``WorkflowState`` into ``runs.state_json`` and checks the
cooperative cancellation flag.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.orchestrator import approval, persistence
from app.orchestrator.evaluator import Evaluator
from app.orchestrator.executor import Executor
from app.orchestrator.planner import Planner
from app.orchestrator.router import normalize_plan
from app.orchestrator.runtime import RunRuntime, WorkflowCancelled
from app.orchestrator.synthesizer import Synthesizer
from app.schemas.workflow import AgentResult, Plan, TaskSpec, TaskStatus, WorkflowState

logger = logging.getLogger(__name__)

RuntimeProvider = Callable[[str], RunRuntime]
RECURSION_LIMIT = 80


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def current_task(state: dict[str, Any]) -> TaskSpec | None:
    task_id = state.get("current_task_id")
    plan = state.get("plan") or {}
    for item in plan.get("tasks") or []:
        if isinstance(item, dict) and item.get("id") == task_id:
            try:
                return TaskSpec.model_validate(item)
            except Exception:  # pragma: no cover - plan is validated on creation
                return None
    return None


def _context(state: dict[str, Any]) -> dict[str, Any]:
    return dict(state.get("context") or {})


def _skipped(state: dict[str, Any]) -> dict[str, str]:
    return dict(_context(state).get("skipped") or {})


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------
def build_graph(
    runtime_provider: RuntimeProvider,
    *,
    checkpointer: MemorySaver | None = None,
):
    """Compile the workflow graph.  ``runtime_provider(run_id) -> RunRuntime``."""
    graph = StateGraph(WorkflowState)

    def node(name: str):
        def register(fn):
            def run(state: dict[str, Any]):
                runtime = runtime_provider(state["run_id"])
                runtime.check_cancelled()
                result = fn(state, runtime)
                if isinstance(result, dict):
                    merged = {**state, **result}
                    runtime.snapshot(merged)
                return result

            graph.add_node(name, run)
            return run

        return register

    # -- intake -----------------------------------------------------------
    @node("intake")
    def intake(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        started_at = state.get("started_at") or now
        fields: dict[str, Any] = {
            "status": "running",
            "token_usage_json": runtime.token_usage(),
        }
        if not state.get("started_at"):
            fields["started_at"] = now_dt
        persistence.update_run(runtime.run_id, **fields)
        runtime.emit(
            "run.started",
            message="Workflow started",
            data={
                "objective": str(state.get("objective") or runtime.objective)[:500],
                "auto_approve": bool(state.get("auto_approve", runtime.auto_approve)),
                "llm_provider": runtime.llm.provider,
                "llm_model": runtime.llm.model,
            },
        )
        return {
            "run_id": runtime.run_id,
            "objective": state.get("objective") or runtime.objective,
            "auto_approve": bool(state.get("auto_approve", runtime.auto_approve)),
            "plan": state.get("plan") or {},
            "task_order": state.get("task_order") or [],
            "current_index": int(state.get("current_index") or 0),
            "current_task_id": None,
            "results": state.get("results") or {},
            "evaluations": state.get("evaluations") or {},
            "attempts": state.get("attempts") or {},
            "feedback": state.get("feedback") or {},
            "approvals": state.get("approvals") or {},
            "pending_approval_id": None,
            "awaiting_task_id": None,
            "final_report": state.get("final_report"),
            "status": "running",
            "error": None,
            "token_usage": runtime.token_usage(),
            "started_at": started_at,
            "completed_at": None,
            "context": _context(state),
        }

    # -- plan -------------------------------------------------------------
    @node("plan")
    def plan_node(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        context = _context(state)
        existing = state.get("plan") or {}
        if existing.get("tasks"):
            try:
                plan = normalize_plan(
                    Plan.model_validate(existing),
                    objective=str(state.get("objective") or runtime.objective),
                    known_tools=_known_tools(runtime),
                )
                source = "resumed"
            except Exception:  # pragma: no cover - corrupt snapshot: replan
                plan, source = self_plan(state, runtime)
        else:
            plan, source = self_plan(state, runtime)
        runtime.save_plan(plan)
        runtime.emit(
            "plan.created",
            message=f"Plan created with {len(plan.tasks)} tasks",
            data={"plan": plan.model_dump(mode="json"), "source": source},
        )
        return {
            "plan": plan.model_dump(mode="json"),
            "task_order": [task.id for task in plan.tasks],
            "current_index": int(state.get("current_index") or 0),
            "current_task_id": None,
            "context": {**context, "plan_source": source},
        }

    def self_plan(state: dict[str, Any], runtime: RunRuntime):
        planner = Planner(runtime, known_tools=_known_tools(runtime))
        plan, source = planner.create_plan(str(state.get("objective") or runtime.objective))
        logger.info(
            "plan_created",
            extra={"run_id": runtime.run_id, "source": source, "tasks": len(plan.tasks)},
        )
        return plan, source

    # -- dispatch (loop head) --------------------------------------------
    @node("dispatch")
    def dispatch(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        plan = Plan.model_validate(state["plan"])
        task_map = plan.task_map()
        order = state.get("task_order") or [task.id for task in plan.tasks]
        results = state.get("results") or {}
        context = _context(state)
        skipped = dict(context.get("skipped") or {})
        index = int(state.get("current_index") or 0)
        while index < len(order):
            task_id = order[index]
            task = task_map.get(task_id)
            if task is None or task_id in results or task_id in skipped:
                index += 1
                continue
            unmet = [
                dep
                for dep in task.depends_on
                if (results.get(dep) or {}).get("status") != "completed"
                or not (state.get("evaluations") or {}).get(dep, {}).get("passed")
            ]
            if unmet:
                skipped[task_id] = f"unmet dependencies: {', '.join(unmet)}"
                persistence.upsert_task(
                    runtime.run_id,
                    task_id,
                    title=task.title,
                    agent=task.agent.value,
                    status="skipped",
                )
                runtime.emit(
                    "agent.completed",
                    agent=task.agent.value,
                    task_id=task_id,
                    message=f"Task {task_id} skipped — dependencies not completed",
                    data={"status": "skipped", "reason": skipped[task_id]},
                )
                logger.info(
                    "task_skipped",
                    extra={"run_id": runtime.run_id, "task_id": task_id, "deps": unmet},
                )
                index += 1
                continue
            break

        if index >= len(order):
            return {
                "current_index": index,
                "current_task_id": None,
                "context": {**context, "skipped": skipped},
            }

        task = task_map[order[index]]
        attempts = dict(state.get("attempts") or {})
        attempt = int(attempts.get(task.id, 0)) + 1
        attempts[task.id] = attempt
        persistence.upsert_task(
            runtime.run_id,
            task.id,
            title=task.title,
            agent=task.agent.value,
            status="running",
            attempts=attempt,
        )
        runtime.emit(
            "task.started",
            agent=task.agent.value,
            task_id=task.id,
            message=f"Starting task {task.id}: {task.title}",
            data={
                "title": task.title,
                "agent": task.agent.value,
                "attempt": attempt,
                "sensitive": task.sensitive,
                "tool_hints": task.tool_hints,
                "depends_on": task.depends_on,
            },
        )
        return {
            "current_index": index,
            "current_task_id": task.id,
            "attempts": attempts,
            "context": {**context, "skipped": skipped},
        }

    # -- route (capability + approval decision) --------------------------
    @node("route")
    def route(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        task = current_task(state)
        if task is None:
            return {"current_task_id": None}
        context = _context(state)
        if approval.needs_approval(task, state, runtime):
            return {"context": {**context, "route": "approval"}}
        approvals = dict(state.get("approvals") or {})
        if task.sensitive:
            approvals[task.id] = "auto_approved"
        return {"approvals": approvals, "context": {**context, "route": "execute"}}

    # -- approval gate ----------------------------------------------------
    @node("approval_request")
    def approval_request(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        task = current_task(state)
        if task is None:  # pragma: no cover - defensive
            return {"current_task_id": None}
        return approval.create_approval_request(runtime, task, state)

    @node("approval_wait")
    def approval_wait(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        task = current_task(state)
        if task is None:  # pragma: no cover - defensive
            return {"current_task_id": None}
        decision = interrupt(
            {
                "approval_id": state.get("pending_approval_id"),
                "task_id": task.id,
                "title": task.title,
                "requested_action": approval.requested_action_text(task),
                "run_id": runtime.run_id,
            }
        )
        value, approval_id, note = _parse_resume_value(decision, state)
        approval.record_decision_in_state(runtime, task, value, approval_id, note)
        approvals = {**(state.get("approvals") or {}), task.id: value}
        return {
            "approvals": approvals,
            "pending_approval_id": None,
            "awaiting_task_id": None,
            "status": "running",
        }

    # -- execute ----------------------------------------------------------
    @node("execute")
    def execute(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        task = current_task(state)
        if task is None:  # pragma: no cover - defensive
            return {"current_task_id": None}
        attempt = int((state.get("attempts") or {}).get(task.id, 1))
        prior = {
            task_id: result
            for task_id, result in (state.get("results") or {}).items()
            if isinstance(result, dict)
        }
        granted = approval.approval_granted(task, state, runtime)
        feedback = str((state.get("feedback") or {}).get(task.id, ""))
        executor = Executor(runtime)
        result = executor.execute(
            task,
            attempt=attempt,
            prior_results=prior,
            approval_granted=granted,
            feedback=feedback,
        )
        results = {**(state.get("results") or {}), task.id: result.model_dump(mode="json")}
        return {"results": results}

    # -- evaluate ---------------------------------------------------------
    @node("evaluate")
    def evaluate(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        task = current_task(state)
        if task is None:  # pragma: no cover - defensive
            return {"current_task_id": None}
        attempt = int((state.get("attempts") or {}).get(task.id, 1))
        raw_result = (state.get("results") or {}).get(task.id) or {}
        try:
            result = AgentResult.model_validate(raw_result)
        except Exception:  # pragma: no cover - defensive
            result = None
        evaluation = Evaluator(runtime).evaluate(task, result, attempt=attempt)
        status = "completed" if evaluation.passed else ("retrying" if evaluation.should_retry else "failed")
        persistence.upsert_task(
            runtime.run_id,
            task.id,
            status=status,
            attempts=attempt,
            evaluation=evaluation.model_dump(mode="json"),
        )
        persistence.record_evaluation_metric(
            runtime.run_id,
            task.id,
            agent=task.agent.value,
            score=evaluation.score,
            passed=evaluation.passed,
            attempts=attempt,
            latency_ms=int((raw_result.get("metrics") or {}).get("latency_ms") or 0),
        )
        runtime.emit(
            "evaluation.completed",
            agent=task.agent.value,
            task_id=task.id,
            message=(
                f"Evaluation {'passed' if evaluation.passed else 'failed'} for {task.id} "
                f"(score {evaluation.score})"
            ),
            data={
                "task_id": task.id,
                "score": evaluation.score,
                "passed": evaluation.passed,
                "criteria_met": evaluation.criteria_met,
                "issues": evaluation.issues,
                "feedback": evaluation.feedback,
                "should_retry": evaluation.should_retry,
                "evaluator": evaluation.evaluator,
                "attempt": attempt,
            },
        )
        return {
            "evaluations": {
                **(state.get("evaluations") or {}),
                task.id: evaluation.model_dump(mode="json"),
            }
        }

    # -- retry / advance / skip ------------------------------------------
    @node("retry")
    def retry(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        task = current_task(state)
        if task is None:  # pragma: no cover - defensive
            return {"current_task_id": None}
        evaluations = state.get("evaluations") or {}
        evaluation = evaluations.get(task.id) or {}
        attempts = dict(state.get("attempts") or {})
        attempts[task.id] = int(attempts.get(task.id, 1)) + 1
        feedback = dict(state.get("feedback") or {})
        feedback[task.id] = str(evaluation.get("feedback") or "")
        persistence.upsert_task(
            runtime.run_id,
            task.id,
            status="running",
            attempts=attempts[task.id],
        )
        runtime.emit(
            "task.retry",
            agent=task.agent.value,
            task_id=task.id,
            message=f"Retrying task {task.id} (attempt {attempts[task.id]})",
            data={
                "task_id": task.id,
                "attempt": attempts[task.id],
                "previous_score": evaluation.get("score"),
                "feedback": feedback[task.id][:600],
            },
        )
        return {"attempts": attempts, "feedback": feedback}

    @node("advance")
    def advance(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        task = current_task(state)
        context = _context(state)
        if task is not None:
            evaluation = (state.get("evaluations") or {}).get(task.id) or {}
            if evaluation and not evaluation.get("passed"):
                failed = list(context.get("failed_tasks") or [])
                if task.id not in failed:
                    failed.append(task.id)
                context["failed_tasks"] = failed
        index = int(state.get("current_index") or 0) + 1
        return {"current_index": index, "current_task_id": None, "context": context}

    @node("skip_task")
    def skip_task(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        task = current_task(state)
        context = _context(state)
        if task is None:  # pragma: no cover - defensive
            return {"current_index": int(state.get("current_index") or 0) + 1}
        skipped = dict(context.get("skipped") or {})
        skipped[task.id] = "approval rejected by human reviewer"
        attempt = int((state.get("attempts") or {}).get(task.id, 1))
        persistence.upsert_task(
            runtime.run_id,
            task.id,
            status="skipped",
            attempts=attempt,
            evaluation={
                "task_id": task.id,
                "score": 0.0,
                "passed": False,
                "issues": ["skipped: approval rejected"],
                "evaluator": "human",
            },
        )
        runtime.emit(
            "agent.completed",
            agent=task.agent.value,
            task_id=task.id,
            message=f"Task {task.id} skipped — approval rejected",
            data={"status": "skipped", "reason": "approval rejected"},
        )
        logger.info(
            "task_skipped_rejected",
            extra={"run_id": runtime.run_id, "task_id": task.id},
        )
        return {
            "current_index": int(state.get("current_index") or 0) + 1,
            "current_task_id": None,
            "context": {**context, "skipped": skipped},
        }

    # -- synth + finalize --------------------------------------------------
    @node("synthesize")
    def synthesize(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        return Synthesizer(runtime).synthesize(state)

    @node("finalize")
    def finalize(state: dict[str, Any], runtime: RunRuntime) -> dict[str, Any]:
        status = str(state.get("status") or "")
        if status == "cancelled":
            runtime.mark_cancelled(state)
            return {}
        results = state.get("results") or {}
        completed = [
            task_id
            for task_id, result in results.items()
            if isinstance(result, dict) and result.get("status") == "completed"
        ]
        if status == "failed" or state.get("error"):
            runtime.finalize_failed(str(state.get("error") or "workflow failed"), state)
            return {}
        if not completed:
            first_error = next(
                (
                    str((result or {}).get("error"))
                    for result in results.values()
                    if isinstance(result, dict) and result.get("error")
                ),
                "",
            )
            message = "No tasks completed successfully."
            if first_error:
                message += f" First failure: {first_error[:600]}"
            runtime.finalize_failed(message, state)
            return {}
        if state.get("final_report"):
            runtime.finalize_completed(state)
        else:
            runtime.finalize_failed("workflow finished without producing a final report", state)
        return {}

    # -- edges ------------------------------------------------------------
    def after_dispatch(state: dict[str, Any]) -> str:
        return "route" if state.get("current_task_id") else "synthesize"

    def after_route(state: dict[str, Any]) -> str:
        return "approval_request" if _context(state).get("route") == "approval" else "execute"

    def after_approval(state: dict[str, Any]) -> str:
        task_id = state.get("current_task_id")
        decision = (state.get("approvals") or {}).get(task_id)
        return "execute" if decision in {"approved", "auto_approved"} else "skip_task"

    def after_evaluate(state: dict[str, Any]) -> str:
        task_id = state.get("current_task_id")
        evaluation = (state.get("evaluations") or {}).get(task_id) or {}
        if evaluation.get("passed"):
            return "advance"
        if evaluation.get("should_retry"):
            return "retry"
        return "advance"

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "plan")
    graph.add_edge("plan", "dispatch")
    graph.add_conditional_edges(
        "dispatch", after_dispatch, {"route": "route", "synthesize": "synthesize"}
    )
    graph.add_conditional_edges(
        "route", after_route, {"approval_request": "approval_request", "execute": "execute"}
    )
    graph.add_edge("approval_request", "approval_wait")
    graph.add_conditional_edges(
        "approval_wait", after_approval, {"execute": "execute", "skip_task": "skip_task"}
    )
    graph.add_edge("execute", "evaluate")
    graph.add_conditional_edges("evaluate", after_evaluate, {"advance": "advance", "retry": "retry"})
    graph.add_edge("retry", "execute")
    graph.add_edge("advance", "dispatch")
    graph.add_edge("skip_task", "dispatch")
    graph.add_edge("synthesize", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())


def _known_tools(runtime: RunRuntime) -> set[str] | None:
    try:
        return set(runtime.registry.tool_names())
    except Exception:  # pragma: no cover - tool discovery defensive
        logger.warning("tool_discovery_failed", extra={"run_id": runtime.run_id})
        return None


def _parse_resume_value(decision: Any, state: dict[str, Any]) -> tuple[str, str, str]:
    """Normalise the value passed to ``Command(resume=...)``."""
    approval_id = str(state.get("pending_approval_id") or "")
    note = ""
    if isinstance(decision, dict):
        value = str(decision.get("decision") or decision.get("status") or "").strip().lower()
        approval_id = str(decision.get("approval_id") or approval_id)
        note = str(decision.get("note") or "")
    else:
        value = str(decision or "").strip().lower()
    if value not in {"approved", "rejected"}:
        logger.warning("invalid_approval_resume_value", extra={"value": value[:50]})
        value = "rejected"  # safe default: never auto-approve on a malformed decision
    return value, approval_id, note


__all__ = ["RECURSION_LIMIT", "build_graph", "current_task"]
