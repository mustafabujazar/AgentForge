"""Human-in-the-loop approval gate.

Flow (LangGraph)
----------------
``route`` decides a sensitive execution task needs a decision →
``approval_request`` (side effects: create Approval row, set run status
``awaiting_approval``, emit ``approval.required``) → ``approval_wait`` node calls
``langgraph.types.interrupt``.  The graph invocation returns, the workflow thread
ends, and the checkpoint stays in the service-owned ``MemorySaver``.

When the API updates the Approval row and calls ``workflow_service.resume_run``,
the service invokes ``graph.invoke(Command(resume=decision), config)``: LangGraph
re-runs only the ``approval_wait`` node, where ``interrupt`` now returns the
decision.  Approved → execute with approval context; rejected → task skipped and
the workflow continues to synthesis (the run still delivers a report with the
limitation documented).

The checkpoint is memory-only; if it is lost (process restart) the service falls
back to rebuilding the graph state from ``runs.state_json`` (see service.py).
"""

from __future__ import annotations

import logging
from typing import Any

from app.orchestrator import persistence
from app.orchestrator.runtime import RunRuntime
from app.schemas.workflow import TaskSpec

logger = logging.getLogger(__name__)

_ACTION_APPROVAL_NOTE = (
    "This task is classified sensitive because it can take real-world actions "
    "(pricing, discounts, campaigns, communications or similar). Approval is required "
    "before the execution agent is allowed to proceed."
)


def needs_approval(task: TaskSpec, state: dict[str, Any], runtime: RunRuntime) -> bool:
    """True when a sensitive task must pause for a human decision."""
    if not task.sensitive:
        return False
    if not bool(getattr(runtime.settings, "hitl_enabled", True)):
        return False
    if bool(state.get("auto_approve", runtime.auto_approve)):
        return False
    decision = (state.get("approvals") or {}).get(task.id)
    return decision not in {"approved", "rejected", "auto_approved"}


def requested_action_text(task: TaskSpec) -> str:
    description = (task.description or "").strip()
    base = f"{task.title}: {description}" if description else task.title
    hints = ", ".join(task.tool_hints) or "remediation actions"
    return f"{base}\n\nTools/actions involved: {hints}"[:2000]


def create_approval_request(
    runtime: RunRuntime,
    task: TaskSpec,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create the Approval row + emit ``approval.required``; return state updates."""
    runtime.check_cancelled()
    requested_action = requested_action_text(task)
    reason = _ACTION_APPROVAL_NOTE
    approval_id = persistence.create_approval(
        runtime.run_id,
        task_id=task.id,
        title=task.title[:255],
        requested_action=requested_action,
        reason=reason,
        risk_level="high" if task.sensitive else "medium",
    )
    runtime.pending_approval_id = approval_id
    # Persist the paused status *before* publishing the event so the API can read the
    # Approval row as soon as a subscriber sees approval.required.
    persistence.update_run(
        runtime.run_id,
        status="awaiting_approval",
        state_json=persistence.json_safe(
            {
                **state,
                "pending_approval_id": approval_id,
                "awaiting_task_id": task.id,
                "status": "awaiting_approval",
            }
        ),
        token_usage_json=runtime.token_usage(),
    )
    runtime.emit(
        "approval.required",
        agent=task.agent.value,
        task_id=task.id,
        message=f"Human approval required for task {task.id}: {task.title}",
        data={
            "approval_id": approval_id,
            "task_id": task.id,
            "title": task.title,
            "requested_action": requested_action,
            "reason": reason,
            "risk_level": "high",
        },
    )
    runtime.emit(
        "run.awaiting_approval",
        agent=task.agent.value,
        task_id=task.id,
        message="Run paused awaiting human approval",
        data={"approval_id": approval_id, "task_id": task.id},
    )
    logger.info(
        "approval_requested",
        extra={"run_id": runtime.run_id, "task_id": task.id, "approval_id": approval_id},
    )
    return {
        "pending_approval_id": approval_id,
        "awaiting_task_id": task.id,
        "status": "awaiting_approval",
        "approvals": {**(state.get("approvals") or {})},
    }


def approval_granted(task: TaskSpec, state: dict[str, Any], runtime: RunRuntime) -> bool:
    """Whether the execution agent may use sensitive/action tools for this task."""
    if not task.sensitive:
        return True  # nothing sensitive; regular tools are unrestricted
    if bool(state.get("auto_approve", runtime.auto_approve)):
        return True
    if not bool(getattr(runtime.settings, "hitl_enabled", True)):
        return True
    return (state.get("approvals") or {}).get(task.id) in {"approved", "auto_approved"}


def record_decision_in_state(
    runtime: RunRuntime,
    task: TaskSpec,
    decision: str,
    approval_id: str,
    note: str = "",
) -> dict[str, Any]:
    """Emit ``approval.resolved`` and return approval state updates."""
    runtime.emit(
        "approval.resolved",
        agent=task.agent.value,
        task_id=task.id,
        message=f"Approval {decision} for task {task.id}",
        data={
            "approval_id": approval_id,
            "task_id": task.id,
            "decision": decision,
            "note": note,
        },
    )
    logger.info(
        "approval_resolved",
        extra={"run_id": runtime.run_id, "task_id": task.id, "decision": decision},
    )
    runtime.pending_approval_id = None
    return {"decision": decision, "approval_id": approval_id, "note": note}


__all__ = [
    "approval_granted",
    "create_approval_request",
    "needs_approval",
    "record_decision_in_state",
    "requested_action_text",
]
