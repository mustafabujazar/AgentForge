"""Run lifecycle API: creation, inspection, events (incl. SSE), approvals, cancel.

Design notes
------------
* ``POST /api/runs`` commits the ``Run`` row and *then* schedules the workflow via
  the frozen orchestrator singleton.  Starting is awaited but returns immediately
  (the service schedules a background worker), so the request never blocks on the
  multi-agent execution.
* The SSE endpoint streams the durable event log first (replay by ``seq``), then
  tails the in-process broker, deduping by ``seq``.  It deliberately opens only
  short-lived sessions (never one session for the whole stream) and closes once
  the run reaches a terminal status and every event has been delivered.
* Approvals/cancel mutate the row first, then poke the orchestrator best-effort.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from contextlib import suppress
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import (
    bearer_scheme,
    get_current_user,
    get_db,
    is_admin,
    resolve_current_user,
    unauthorized,
)
from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.events import broker
from app.db.base import get_session_factory
from app.db.models import Approval, Artifact, Run, RunEvent, TaskRecord, User, utcnow
from app.schemas.run import (
    ApprovalDecision,
    ArtifactRead,
    PendingApprovalRead,
    ReportResponse,
    RunCreate,
    RunEventRead,
    RunListResponse,
    RunRead,
    TaskRead,
)
from app.schemas.workflow import RunStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])

TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)
_TERMINAL_LABEL = "/".join(sorted(TERMINAL_STATUSES))

# Resolved lazily on the first workflow start so importing the API never fails
# while the orchestrator module is still being written.  Tests inject a stub by
# monkeypatching this module attribute.
workflow_service: Any | None = None

_HEARTBEAT_SECONDS = 15.0
_POLL_SECONDS = 1.0
_REPLAY_BATCH = 500


# ---------------------------------------------------------------------------
# Orchestrator access
# ---------------------------------------------------------------------------
def _get_workflow_service() -> Any:
    """Return the orchestrator service singleton (imported on first use)."""
    global workflow_service
    if workflow_service is None:
        from app.orchestrator.service import workflow_service as service

        workflow_service = service
    return workflow_service


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------
def _pending_approval_read(row: Approval) -> PendingApprovalRead:
    return PendingApprovalRead(
        id=row.id,
        task_id=row.task_id,
        title=row.title or "",
        requested_action=row.requested_action or "",
        reason=row.reason or "",
    )


def _pending_approval_for_run(db: Session, run_id: str) -> PendingApprovalRead | None:
    row = db.scalars(
        select(Approval)
        .where(Approval.run_id == run_id, Approval.status == "pending")
        .order_by(Approval.created_at.desc())
        .limit(1)
    ).first()
    return _pending_approval_read(row) if row is not None else None


def _pending_for_runs(db: Session, run_ids: list[str]) -> dict[str, PendingApprovalRead]:
    if not run_ids:
        return {}
    rows = db.scalars(
        select(Approval)
        .where(Approval.run_id.in_(run_ids), Approval.status == "pending")
        .order_by(Approval.created_at.desc())
    ).all()
    pending: dict[str, PendingApprovalRead] = {}
    for row in rows:
        pending.setdefault(row.run_id, _pending_approval_read(row))
    return pending


def _run_read(run: Run, pending: PendingApprovalRead | None = None) -> RunRead:
    return RunRead.from_run(run, pending)


def _artifact_read(artifact: Artifact) -> ArtifactRead:
    return ArtifactRead(
        id=artifact.id,
        run_id=artifact.run_id,
        name=artifact.name,
        kind=artifact.kind or "markdown",
        path=artifact.path or "",
        description=artifact.description or "",
        created_at=artifact.created_at,
    )


def _get_owned_run(db: Session, run_id: str, user: User) -> Run:
    """Fetch a run, enforcing ownership (admins may see every run)."""
    run = db.get(Run, run_id)
    if run is None or (not is_admin(user) and run.user_id != user.id):
        # 404 rather than 403 so run ids cannot be probed across tenants.
        raise NotFoundError("run not found")
    return run


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.post("", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    payload: RunCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunRead:
    """Create a run row and hand it to the orchestrator (non-blocking)."""
    settings = get_settings()
    auto_approve = (
        payload.auto_approve
        if payload.auto_approve is not None
        else bool(settings.auto_approve_default)
    )
    objective = payload.objective.strip()
    run = Run(
        user_id=current_user.id,
        objective=objective,
        dataset=payload.dataset,
        status=RunStatus.QUEUED.value,
        auto_approve=bool(auto_approve),
        token_usage_json={},
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    logger.info("run_created", extra={"run_id": run.id, "user_id": current_user.id})

    try:
        service = _get_workflow_service()
        # Contract: returns as soon as the background execution is scheduled.
        await service.start_run(run.id, objective)
    except (ImportError, AttributeError) as exc:
        # Orchestrator not wired in this checkout yet: keep the row queued so it
        # can be started later instead of losing the user's request.
        logger.warning(
            "workflow_service_unavailable",
            extra={"run_id": run.id, "error": str(exc)},
        )
    except Exception as exc:  # start failed for real: surface it on the run
        logger.warning("workflow_start_failed", extra={"run_id": run.id, "error": str(exc)})
        run.status = RunStatus.FAILED.value
        run.error = f"failed to start workflow: {exc}"[:2000]
        run.completed_at = utcnow()
        db.add(run)
        db.commit()
        db.refresh(run)

    return _run_read(run, _pending_approval_for_run(db, run.id))


@router.get("", response_model=RunListResponse)
def list_runs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: RunStatus | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunListResponse:
    """Paginated run list; members only see their own runs."""
    conditions = []
    if not is_admin(current_user):
        conditions.append(Run.user_id == current_user.id)
    if status_filter is not None:
        conditions.append(Run.status == status_filter.value)

    count_stmt = select(func.count()).select_from(Run)
    rows_stmt = select(Run)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        rows_stmt = rows_stmt.where(condition)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(
        rows_stmt.order_by(Run.created_at.desc(), Run.id.desc()).offset(offset).limit(limit)
    ).all()
    pending = _pending_for_runs(db, [row.id for row in rows])
    return RunListResponse(items=[_run_read(row, pending.get(row.id)) for row in rows], total=total)


@router.get("/{run_id}", response_model=RunRead)
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunRead:
    """Run detail including plan, token usage and any pending approval."""
    run = _get_owned_run(db, run_id, current_user)
    return _run_read(run, _pending_approval_for_run(db, run.id))


@router.get("/{run_id}/tasks", response_model=list[TaskRead])
def get_run_tasks(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TaskRead]:
    """Task records in creation order."""
    run = _get_owned_run(db, run_id, current_user)
    rows = db.scalars(
        select(TaskRecord)
        .where(TaskRecord.run_id == run.id)
        .order_by(TaskRecord.created_at, TaskRecord.task_id)
    ).all()
    return [TaskRead.from_record(row) for row in rows]


@router.get("/{run_id}/report", response_model=ReportResponse)
def get_run_report(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReportResponse:
    """Final Markdown report plus the run's artifacts."""
    run = _get_owned_run(db, run_id, current_user)
    artifacts = db.scalars(
        select(Artifact).where(Artifact.run_id == run.id).order_by(Artifact.created_at)
    ).all()
    return ReportResponse(
        run_id=run.id,
        report=run.final_report,
        artifacts=[_artifact_read(artifact) for artifact in artifacts],
    )


# ---------------------------------------------------------------------------
# Events: plain polling endpoint + live SSE tail
# ---------------------------------------------------------------------------
@router.get("/{run_id}/events", response_model=list[RunEventRead])
def get_run_events(
    run_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[RunEventRead]:
    """Durable event log in ``seq`` order; used as the SSE fallback path."""
    run = _get_owned_run(db, run_id, current_user)
    rows = db.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run.id, RunEvent.seq > after_seq)
        .order_by(RunEvent.seq)
        .limit(limit)
    ).all()
    return [RunEventRead.from_event(row) for row in rows]


def _authenticate_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[str, bool]:
    """Resolve Bearer credentials using a short session (safe for SSE)."""
    if credentials is None or not credentials.credentials:
        raise unauthorized("missing bearer token")
    session = get_session_factory()()
    try:
        user = resolve_current_user(credentials.credentials, session)
        return user.id, is_admin(user)
    finally:
        session.close()


def _assert_run_access(run_id: str, user_id: str, admin: bool) -> str:
    """Verify the caller may stream this run; return its current status."""
    session = get_session_factory()()
    try:
        run = session.get(Run, run_id)
        if run is None or (not admin and run.user_id != user_id):
            raise NotFoundError("run not found")
        return run.status
    finally:
        session.close()


def _poll_events(run_id: str, after_seq: int) -> tuple[list[RunEventRead], str | None, int]:
    """Short-session read of new durable events + run status + max known seq."""
    session = get_session_factory()()
    try:
        rows = session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
            .order_by(RunEvent.seq)
            .limit(_REPLAY_BATCH)
        ).all()
        events = [RunEventRead.from_event(row) for row in rows]
        run_status = session.scalar(select(Run.status).where(Run.id == run_id))
        max_seq = session.scalar(
            select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
        )
        return events, run_status, int(max_seq or 0)
    finally:
        session.close()


async def _pump_events(run_id: str, queue: "asyncio.Queue[dict[str, Any]]") -> None:
    """Bridge the synchronous broker subscription into an asyncio queue."""
    try:
        async for event in broker.subscribe(run_id):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(event)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - defensive
        logger.exception("sse_broker_pump_failed", extra={"run_id": run_id})


def _sse_frame(event: RunEventRead) -> str:
    return f"event: message\ndata: {event.model_dump_json()}\n\n"


def _coerce_seq(value: Any) -> int | None:
    try:
        seq = int(value)
    except (TypeError, ValueError):
        return None
    return seq if seq > 0 else None


def _live_frame(
    raw: Any, run_id: str, last_sent: int
) -> tuple[str | None, int]:
    """Convert a broker event dict into an SSE frame, deduping by ``seq``."""
    if not isinstance(raw, dict):
        return None, last_sent
    seq = _coerce_seq(raw.get("seq"))
    if seq is None or seq <= last_sent:
        return None, last_sent
    ts_raw = raw.get("ts")
    ts: datetime | None = None
    if isinstance(ts_raw, datetime):
        ts = ts_raw
    elif isinstance(ts_raw, str) and ts_raw.strip():
        try:
            ts = datetime.fromisoformat(ts_raw.strip().replace("Z", "+00:00"))
        except ValueError:
            ts = None
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {} if data is None else {"value": data}
    duration = raw.get("duration_ms")
    try:
        duration_ms = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_ms = None
    agent = raw.get("agent")
    if agent is not None and not isinstance(agent, str):
        agent = getattr(agent, "value", str(agent))
    task_id = raw.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        task_id = str(task_id)
    try:
        event = RunEventRead(
            seq=seq,
            run_id=str(raw.get("run_id") or run_id),
            type=str(raw.get("type") or "message"),
            ts=ts,
            agent=agent,
            task_id=task_id,
            message=str(raw.get("message") or ""),
            data=data,
            duration_ms=duration_ms,
        )
    except Exception:  # pragma: no cover - malformed broker payload
        logger.warning("sse_live_event_skipped", extra={"run_id": run_id, "seq": seq})
        return None, last_sent
    return _sse_frame(event), seq


async def _event_stream(run_id: str, after_seq: int) -> AsyncIterator[str]:
    """Replay persisted events, then tail the broker until the run is terminal."""
    last_sent = max(0, int(after_seq or 0))
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)
    pump_task = asyncio.create_task(_pump_events(run_id, queue))
    next_heartbeat = time.monotonic() + _HEARTBEAT_SECONDS
    try:
        # (1) Replay durable history so reconnects with ?after_seq never miss events.
        history, _status, _max_seq = await run_in_threadpool(_poll_events, run_id, last_sent)
        for event in history:
            if event.seq <= last_sent:
                continue
            last_sent = event.seq
            yield _sse_frame(event)

        while True:
            emitted = 0

            # (2) Drain the live broker tail.
            while True:
                try:
                    live = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                frame, seq = _live_frame(live, run_id, last_sent)
                if frame is not None:
                    last_sent = seq
                    emitted += 1
                    yield frame

            if not emitted:
                # (3) Gap fill from the durable log + terminal check.
                history, run_status, max_seq = await run_in_threadpool(
                    _poll_events, run_id, last_sent
                )
                for event in history:
                    if event.seq <= last_sent:
                        continue
                    last_sent = event.seq
                    emitted += 1
                    yield _sse_frame(event)
                if run_status is None:
                    break  # run row vanished; nothing left to stream
                if run_status in TERMINAL_STATUSES and int(max_seq or 0) <= last_sent:
                    # Final drain of anything published while we were reading.
                    while True:
                        try:
                            live = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        frame, seq = _live_frame(live, run_id, last_sent)
                        if frame is not None:
                            last_sent = seq
                            yield frame
                    if queue.empty():
                        break

            # (4) Heartbeat comment keeps proxies and idle clients alive.
            now = time.monotonic()
            if now >= next_heartbeat:
                next_heartbeat = now + _HEARTBEAT_SECONDS
                yield ": ping\n\n"

            # (5) Wait briefly for a live event; bounded so terminal runs close fast.
            try:
                live = await asyncio.wait_for(queue.get(), timeout=_POLL_SECONDS)
            except asyncio.TimeoutError:
                continue
            frame, seq = _live_frame(live, run_id, last_sent)
            if frame is not None:
                last_sent = seq
                yield frame
    finally:
        pump_task.cancel()


@router.get("/{run_id}/events/stream", name="stream_run_events")
async def stream_run_events(
    run_id: str,
    after_seq: int = Query(0, ge=0),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    """Server-Sent Events tail of a run (Bearer auth, fetch + ReadableStream)."""
    user_id, admin = await run_in_threadpool(_authenticate_token, credentials)
    await run_in_threadpool(_assert_run_access, run_id, user_id, admin)
    return StreamingResponse(
        _event_stream(run_id, after_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Mutations: cancel + human-in-the-loop approvals
# ---------------------------------------------------------------------------
@router.post("/{run_id}/cancel", response_model=RunRead)
async def cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunRead:
    """Request cooperative cancellation and mark the run cancelled."""
    run = _get_owned_run(db, run_id, current_user)
    if run.status in TERMINAL_STATUSES:
        return _run_read(run, _pending_approval_for_run(db, run.id))

    try:
        service = _get_workflow_service()
        result = service.cancel(run.id)  # sync by contract, tolerate async impls
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        # Cancellation is best-effort: the DB status below is authoritative.
        logger.warning("workflow_cancel_failed", extra={"run_id": run.id, "error": str(exc)})

    # If the workflow worker already wrote a terminal status (e.g. it finished
    # while we were cancelling), leave that outcome untouched.
    db.refresh(run)
    if run.status in TERMINAL_STATUSES:
        return _run_read(run, _pending_approval_for_run(db, run.id))

    run.status = RunStatus.CANCELLED.value
    run.completed_at = utcnow()
    db.add(run)
    db.commit()
    db.refresh(run)
    logger.info("run_cancelled", extra={"run_id": run.id})
    return _run_read(run, _pending_approval_for_run(db, run.id))


@router.post("/{run_id}/approvals/{approval_id}", response_model=RunRead)
async def decide_approval(
    run_id: str,
    approval_id: str,
    payload: ApprovalDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunRead:
    """Approve or reject a pending sensitive action, then resume the workflow."""
    run = _get_owned_run(db, run_id, current_user)
    approval = db.get(Approval, approval_id)
    if approval is None or approval.run_id != run.id:
        raise NotFoundError("approval not found")
    if (approval.status or "").strip().lower() != "pending":
        raise ConflictError("approval has already been decided")
    if run.status in TERMINAL_STATUSES:
        raise ConflictError("run is already finished")

    approval.status = payload.decision
    approval.decided_by = current_user.id
    approval.note = payload.note
    approval.decided_at = utcnow()
    db.add(approval)
    db.commit()
    db.refresh(approval)
    logger.info(
        "approval_decided",
        extra={"run_id": run.id, "approval_id": approval.id, "decision": payload.decision},
    )

    try:
        service = _get_workflow_service()
        # Contract: schedules the resume and returns promptly.
        await service.resume_run(run.id)
    except Exception as exc:
        logger.warning("workflow_resume_failed", extra={"run_id": run.id, "error": str(exc)})
        run.status = RunStatus.FAILED.value
        run.error = f"failed to resume workflow after approval: {exc}"[:2000]
        run.completed_at = utcnow()
        db.add(run)
        db.commit()

    db.refresh(run)
    return _run_read(run, _pending_approval_for_run(db, run.id))


__all__ = ["router", "workflow_service"]
