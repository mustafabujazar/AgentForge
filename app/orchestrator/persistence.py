"""Transactional persistence helpers used by the workflow service.

All functions are deliberately small and open a *fresh* SQLAlchemy session per
operation (``session_scope``) because the workflow runs on its own thread while the
API may concurrently read the same SQLite database.  Models are configured with
``expire_on_commit=False`` so detached instances remain readable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db.base import session_scope
from app.db.models import Approval, Artifact, EvaluationMetric, Run, RunEvent, TaskRecord, new_id, utcnow

logger = logging.getLogger(__name__)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
    """Round-trip ``value`` through JSON so it can be stored in a JSON column."""
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {"repr": repr(value)}


# ---------------------------------------------------------------------------
# Run rows
# ---------------------------------------------------------------------------
def ensure_run(
    run_id: str,
    objective: str,
    *,
    user_id: str | None = None,
    auto_approve: bool = False,
) -> bool:
    """Make sure a ``runs`` row exists.  Returns True when a row was created."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is not None:
            if objective and not run.objective:
                run.objective = objective
            return False
        session.add(
            Run(
                id=run_id,
                user_id=user_id,
                objective=objective,
                status="queued",
                auto_approve=bool(auto_approve),
                token_usage_json={},
            )
        )
        return True


def get_run(run_id: str) -> Run | None:
    with session_scope() as session:
        return session.get(Run, run_id)


def update_run(run_id: str, **fields: Any) -> None:
    """Update columns on a run row (silently ignores unknown/missing runs)."""
    if not fields:
        return
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            logger.warning("update_run_missing_run", extra={"run_id": run_id})
            return
        for key, value in fields.items():
            if key in {"plan_json", "state_json", "token_usage_json"} and value is not None:
                value = json_safe(value)
            if hasattr(run, key):
                setattr(run, key, value)


def load_state(run_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None or not run.state_json:
            return None
        return dict(run.state_json)


def run_status(run_id: str) -> tuple[str, dict[str, Any] | None]:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return "missing", None
        return run.status, (dict(run.token_usage_json or {}))


# ---------------------------------------------------------------------------
# Task records
# ---------------------------------------------------------------------------
def upsert_task(
    run_id: str,
    task_id: str,
    *,
    title: str = "",
    agent: str = "",
    status: str | None = None,
    attempts: int | None = None,
    result: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
) -> None:
    """Insert or update the ``task_records`` row for ``(run_id, task_id)``."""
    try:
        with session_scope() as session:
            record = session.execute(
                select(TaskRecord).where(
                    TaskRecord.run_id == run_id, TaskRecord.task_id == task_id
                )
            ).scalar_one_or_none()
            if record is None:
                record = TaskRecord(
                    id=new_id(),
                    run_id=run_id,
                    task_id=task_id,
                    title=title,
                    agent=agent,
                    status=status or "pending",
                    attempts=attempts or 0,
                )
                session.add(record)
            else:
                if title:
                    record.title = title
                if agent:
                    record.agent = agent
            if status is not None:
                record.status = status
            if attempts is not None:
                record.attempts = attempts
            if result is not None:
                record.result_json = json_safe(result)
            if evaluation is not None:
                record.evaluation_json = json_safe(evaluation)
    except IntegrityError:  # pragma: no cover - concurrent upsert, retry once
        logger.warning("task_upsert_conflict", extra={"run_id": run_id, "task_id": task_id})
        with session_scope() as session:
            record = session.execute(
                select(TaskRecord).where(
                    TaskRecord.run_id == run_id, TaskRecord.task_id == task_id
                )
            ).scalar_one_or_none()
            if record is not None and status is not None:
                record.status = status


def get_tasks(run_id: str) -> list[TaskRecord]:
    with session_scope() as session:
        return list(
            session.execute(select(TaskRecord).where(TaskRecord.run_id == run_id)).scalars()
        )


# ---------------------------------------------------------------------------
# Evaluation metrics
# ---------------------------------------------------------------------------
def record_evaluation_metric(
    run_id: str,
    task_id: str,
    *,
    agent: str,
    score: float,
    passed: bool,
    attempts: int,
    latency_ms: int = 0,
) -> None:
    try:
        with session_scope() as session:
            session.add(
                EvaluationMetric(
                    run_id=run_id,
                    task_id=task_id,
                    agent=agent,
                    score=float(score),
                    passed=bool(passed),
                    attempts=int(attempts),
                    latency_ms=int(latency_ms),
                )
            )
    except SQLAlchemyError:  # pragma: no cover - metrics must never break a run
        logger.warning("evaluation_metric_write_failed", extra={"run_id": run_id})


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------
def create_artifact(
    run_id: str,
    *,
    name: str,
    description: str = "",
    kind: str = "markdown",
    path: str = "",
    content: str | None = None,
) -> str:
    artifact_id = new_id()
    with session_scope() as session:
        session.add(
            Artifact(
                id=artifact_id,
                run_id=run_id,
                name=name,
                kind=kind,
                path=path,
                content=content,
                description=description,
            )
        )
    return artifact_id


def update_artifact_content(artifact_id: str, content: str) -> None:
    with session_scope() as session:
        artifact = session.get(Artifact, artifact_id)
        if artifact is not None:
            artifact.content = content


def ensure_artifact(
    run_id: str,
    *,
    name: str,
    description: str = "",
    kind: str = "markdown",
    path: str = "",
    content: str | None = None,
) -> str | None:
    """Create an artifact row unless one already exists for this run + path."""
    with session_scope() as session:
        if path:
            existing = session.execute(
                select(Artifact).where(Artifact.run_id == run_id, Artifact.path == path)
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id
        artifact_id = new_id()
        session.add(
            Artifact(
                id=artifact_id,
                run_id=run_id,
                name=name[:255],
                kind=kind[:32] or "markdown",
                path=(path or "")[:1024],
                content=content,
                description=description,
            )
        )
        return artifact_id


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------
def create_approval(
    run_id: str,
    *,
    task_id: str,
    title: str,
    requested_action: str,
    reason: str = "",
    risk_level: str = "medium",
) -> str:
    approval_id = new_id()
    with session_scope() as session:
        session.add(
            Approval(
                id=approval_id,
                run_id=run_id,
                task_id=task_id,
                title=title,
                requested_action=requested_action,
                reason=reason,
                risk_level=risk_level,
                status="pending",
            )
        )
    return approval_id


def latest_approval(run_id: str) -> Approval | None:
    with session_scope() as session:
        return session.execute(
            select(Approval)
            .where(Approval.run_id == run_id)
            .order_by(Approval.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def max_event_seq(run_id: str) -> int:
    with session_scope() as session:
        value = session.execute(
            select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
        ).scalar()
    return int(value or 0)


def append_event(
    run_id: str,
    *,
    seq: int,
    event_type: str,
    agent: str | None = None,
    task_id: str | None = None,
    message: str = "",
    data: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    with session_scope() as session:
        session.add(
            RunEvent(
                run_id=run_id,
                seq=int(seq),
                type=event_type,
                agent=agent,
                task_id=task_id,
                message=(message or "")[:8000],
                data_json=json_safe(data or {}),
                duration_ms=duration_ms,
            )
        )


def event_exists(run_id: str, seq: int) -> bool:  # pragma: no cover - diagnostic helper
    with session_scope() as session:
        return (
            session.execute(
                select(RunEvent.id).where(RunEvent.run_id == run_id, RunEvent.seq == seq)
            ).first()
            is not None
        )


__all__ = [
    "append_event",
    "create_approval",
    "create_artifact",
    "ensure_run",
    "get_run",
    "get_tasks",
    "json_safe",
    "latest_approval",
    "load_state",
    "max_event_seq",
    "record_evaluation_metric",
    "update_run",
    "upsert_task",
    "utcnow",
    "utcnow_iso",
]
