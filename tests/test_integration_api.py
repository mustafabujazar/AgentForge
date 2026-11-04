"""HTTP integration tests for the AgentForge FastAPI layer.

These tests exercise real routers, real SQLAlchemy persistence (isolated temp
SQLite DB from ``tests/conftest.py``) and real JWT auth.  Everything below the
HTTP boundary that needs an LLM or background workflow is stubbed:

* ``POST /api/runs`` is served by :class:`StubWorkflowService`, injected through
  the documented seam ``app.api.runs.workflow_service``.  No planner, agent or
  LLM is invoked, so the API tests are fast and deterministic.
* ``app.api.runs`` only stores the run row; tasks/events/reports/artifacts are
  seeded directly through SQLAlchemy to model workflow outputs.

Network access is blocked session-wide by the socket guard in conftest.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import (
    Approval,
    Artifact,
    EvaluationMetric,
    Run,
    RunEvent,
    TaskRecord,
    User,
    utcnow,
)

pytestmark = [pytest.mark.integration, pytest.mark.offline]

DEMO_OBJECTIVE = (
    "Analyze our sales performance, identify the three worst-performing products, "
    "investigate why they are underperforming, and generate a report with recommendations."
)
SECOND_OBJECTIVE = "Build a prioritized action plan to fix the three worst-performing products."

TERMINAL = {"completed", "failed", "cancelled"}


# ---------------------------------------------------------------------------
# Stub workflow service (the documented test seam)
# ---------------------------------------------------------------------------
class StubWorkflowService:
    """Records lifecycle calls without running the LangGraph workflow or any LLM."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.resumed: list[str] = []
        self.cancelled: list[str] = []
        self.fail_start: Exception | None = None
        self.fail_resume: Exception | None = None

    async def start_run(self, run_id: str, objective: str) -> None:
        self.started.append((run_id, objective))
        if self.fail_start is not None:
            raise self.fail_start

    async def resume_run(self, run_id: str) -> None:
        self.resumed.append(run_id)
        if self.fail_resume is not None:
            raise self.fail_resume

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


@pytest.fixture
def stub_workflow(monkeypatch) -> StubWorkflowService:
    """Inject a no-op workflow service into the runs router."""
    import app.api.runs as runs_api

    stub = StubWorkflowService()
    monkeypatch.setattr(runs_api, "workflow_service", stub)
    return stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bearer(registration: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {registration['access_token']}"}


def register(
    client,
    email: str | None = None,
    password: str = "password123",
    full_name: str = "QA Member",
) -> dict[str, Any]:
    email = email or f"qa-{uuid.uuid4().hex[:10]}@example.com"
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "full_name": full_name},
    )
    assert response.status_code in (200, 201), response.text
    body = response.json()
    body["email"] = email
    return body


def create_run(
    client,
    headers: dict[str, str],
    objective: str = DEMO_OBJECTIVE,
    *,
    auto_approve: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"objective": objective}
    if auto_approve is not None:
        payload["auto_approve"] = auto_approve
    response = client.post("/api/runs", json=payload, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()


def refresh_run(db_session, run_id: str) -> Run:
    db_session.expire_all()
    run = db_session.get(Run, run_id)
    assert run is not None, f"run {run_id} missing"
    return run


def update_run(db_session, run_id: str, **fields: Any) -> Run:
    run = refresh_run(db_session, run_id)
    for key, value in fields.items():
        setattr(run, key, value)
    db_session.add(run)
    db_session.commit()
    db_session.expire_all()
    return db_session.get(Run, run_id)


def add_task(
    db_session,
    run_id: str,
    task_id: str,
    *,
    title: str = "Analyze",
    agent: str = "analyst",
    status: str = "completed",
    attempts: int = 1,
    result: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
) -> TaskRecord:
    record = TaskRecord(
        run_id=run_id,
        task_id=task_id,
        title=title,
        agent=agent,
        status=status,
        attempts=attempts,
        result_json=result,
        evaluation_json=evaluation,
    )
    db_session.add(record)
    db_session.commit()
    return record


def add_event(
    db_session,
    run_id: str,
    seq: int,
    event_type: str,
    *,
    agent: str | None = "analyst",
    task_id: str | None = None,
    message: str = "",
    data: dict[str, Any] | None = None,
) -> RunEvent:
    record = RunEvent(
        run_id=run_id,
        seq=seq,
        type=event_type,
        agent=agent,
        task_id=task_id,
        message=message,
        data_json=data or {},
    )
    db_session.add(record)
    db_session.commit()
    return record


def add_artifact(
    db_session,
    run_id: str,
    *,
    name: str = "report.md",
    kind: str = "markdown",
    content: str | None = None,
    path: str = "",
    description: str = "",
) -> Artifact:
    record = Artifact(
        run_id=run_id,
        name=name,
        kind=kind,
        path=path,
        content=content,
        description=description,
    )
    db_session.add(record)
    db_session.commit()
    return record


def add_approval(
    db_session,
    run_id: str,
    *,
    task_id: str = "task-2",
    title: str = "Apply remediation actions",
    requested_action: str = "Apply a 5% discount to the three underperforming products",
    reason: str = "Sensitive execution task requires approval",
    status: str = "pending",
) -> Approval:
    record = Approval(
        run_id=run_id,
        task_id=task_id,
        title=title,
        requested_action=requested_action,
        reason=reason,
        risk_level="high",
        status=status,
    )
    db_session.add(record)
    db_session.commit()
    return record


def parse_sse_frames(body: str) -> list[dict[str, Any]]:
    """Extract ``data: {json}`` payloads from an SSE body."""
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload:
                events.append(json.loads(payload))
    return events


# ===========================================================================
# Health / meta / contract shape
# ===========================================================================
class TestHealthAndContract:
    def test_health(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"]
        assert body["llm_provider"] == "mock"
        assert body["database"] == "sqlite"
        assert body["environment"] == "test"
        assert body["llm_model"]

    def test_api_root(self, client):
        response = client.get("/api")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "AgentForge"
        assert body["status"] == "ok"
        assert body["version"]

    def test_spa_or_json_root(self, client):
        response = client.get("/")
        assert response.status_code == 200
        content_type = response.headers.get("content-type", "")
        assert "text/html" in content_type or "application/json" in content_type

    def test_openapi_contains_contract_paths(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        required: dict[str, set[str]] = {
            "/api/auth/register": {"post"},
            "/api/auth/login": {"post"},
            "/api/auth/me": {"get"},
            "/api/runs": {"post", "get"},
            "/api/runs/{run_id}": {"get"},
            "/api/runs/{run_id}/tasks": {"get"},
            "/api/runs/{run_id}/events": {"get"},
            "/api/runs/{run_id}/events/stream": {"get"},
            "/api/runs/{run_id}/report": {"get"},
            "/api/runs/{run_id}/artifacts": {"get"},
            "/api/runs/{run_id}/cancel": {"post"},
            "/api/runs/{run_id}/approvals/{approval_id}": {"post"},
            "/api/artifacts/{artifact_id}/download": {"get"},
            "/api/agents": {"get"},
            "/api/tools": {"get"},
            "/api/knowledge/ingest": {"post"},
            "/api/knowledge/search": {"get"},
            "/api/health": {"get"},
            "/api/metrics/runs": {"get"},
        }
        missing_paths = sorted(set(required) - set(paths))
        assert not missing_paths, f"missing contract paths: {missing_paths}"
        missing_methods = {
            path: sorted(methods - set(paths[path]))
            for path, methods in required.items()
            if methods - set(paths[path])
        }
        assert not missing_methods, f"missing contract methods: {missing_methods}"


# ===========================================================================
# Auth
# ===========================================================================
class TestAuth:
    def test_register_returns_token_and_user(self, client):
        registration = register(client, email="new.user@example.com")
        assert registration["token_type"] == "bearer"
        assert registration["access_token"]
        assert registration["user"]["email"] == "new.user@example.com"
        assert registration["user"]["role"] == "member"
        assert registration["user"]["full_name"] == "QA Member"
        assert registration["user"]["id"]

    def test_register_duplicate_email_conflicts(self, client):
        register(client, email="dup@example.com")
        response = client.post(
            "/api/auth/register",
            json={"email": "DUP@example.com", "password": "password123", "full_name": "x"},
        )
        assert response.status_code == 409
        assert "already registered" in response.json()["detail"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"email": "not-an-email", "password": "password123"},
            {"email": "ok@example.com", "password": "short"},
            {"email": "ok@example.com"},
        ],
    )
    def test_register_validation(self, client, payload):
        response = client.post("/api/auth/register", json=payload)
        assert response.status_code == 422

    def test_login_success(self, client):
        registration = register(client, email="login@example.com")
        response = client.post(
            "/api/auth/login",
            json={"email": "LOGIN@example.com", "password": "password123"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"]
        assert body["token_type"] == "bearer"
        assert body["user"]["id"] == registration["user"]["id"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"email": "login@example.com", "password": "wrong-password"},
            {"email": "missing@example.com", "password": "password123"},
        ],
    )
    def test_login_failure_is_401(self, client, payload):
        register(client, email="login@example.com")
        response = client.post("/api/auth/login", json=payload)
        assert response.status_code == 401
        assert response.headers.get("www-authenticate") == "Bearer"

    def test_me_returns_authenticated_user(self, client, demo_user):
        assert demo_user["email"] == "demo@agentforge.io"
        assert demo_user["role"] == "admin"
        assert demo_user["id"]

    @pytest.mark.parametrize(
        "headers",
        [None, {"Authorization": "Bearer "}, {"Authorization": "Bearer garbage.token.value"}],
    )
    def test_me_requires_valid_token(self, client, headers):
        response = client.get("/api/auth/me", headers=headers or {})
        assert response.status_code == 401

    def test_me_rejects_expired_token(self, client, demo_user):
        from app.core.security import create_access_token

        expired = create_access_token(demo_user["id"], expires_minutes=-5)
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
        assert response.status_code == 401

    def test_me_rejects_token_for_deleted_user(self, client):
        from app.core.security import create_access_token

        token = create_access_token("user-that-does-not-exist")
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


# ===========================================================================
# Runs: create / list / get / tasks / report / artifacts
# ===========================================================================
class TestRuns:
    def test_create_run_schedules_workflow(self, client, auth_headers, stub_workflow):
        run = create_run(client, auth_headers, auto_approve=True)
        assert run["id"]
        assert run["objective"] == DEMO_OBJECTIVE
        assert run["status"] == "queued"
        assert run["plan"] is None
        assert run["final_report"] is None
        assert run["error"] is None
        assert run["pending_approval"] is None
        assert run["token_usage"]["total_tokens"] == 0
        assert stub_workflow.started == [(run["id"], DEMO_OBJECTIVE)]

    def test_create_run_requires_auth(self, client):
        response = client.post("/api/runs", json={"objective": DEMO_OBJECTIVE})
        assert response.status_code == 401

    def test_create_run_validation(self, client, auth_headers):
        response = client.post("/api/runs", json={"objective": "too short"}, headers=auth_headers)
        assert response.status_code == 422
        response = client.post("/api/runs", json={}, headers=auth_headers)
        assert response.status_code == 422

    def test_create_run_survives_service_start_failure(self, client, auth_headers, stub_workflow):
        stub_workflow.fail_start = RuntimeError("orchestrator exploded")
        response = client.post("/api/runs", json={"objective": DEMO_OBJECTIVE}, headers=auth_headers)
        assert response.status_code == 202
        assert response.json()["status"] == "failed"
        assert "failed to start workflow" in response.json()["error"]

    def test_list_runs_pagination_and_order(self, client, auth_headers, stub_workflow):
        first = create_run(client, auth_headers)
        second = create_run(client, auth_headers, SECOND_OBJECTIVE)
        response = client.get("/api/runs", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert [item["id"] for item in body["items"]] == [second["id"], first["id"]]

        page = client.get("/api/runs?limit=1&offset=1", headers=auth_headers).json()
        assert page["total"] == 2
        assert [item["id"] for item in page["items"]] == [first["id"]]

    def test_list_runs_status_filter(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        create_run(client, auth_headers, SECOND_OBJECTIVE)
        update_run(db_session, run["id"], status="completed", completed_at=utcnow())
        response = client.get("/api/runs?status=completed", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == run["id"]
        assert body["items"][0]["status"] == "completed"

    def test_get_run_detail_with_plan_and_tokens(
        self, client, auth_headers, stub_workflow, db_session
    ):
        run = create_run(client, auth_headers)
        plan = {
            "objective": DEMO_OBJECTIVE,
            "summary": "Three analyses plus a report",
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Rank products",
                    "description": "Find the bottom three",
                    "agent": "analyst",
                },
                {
                    "id": "task-2",
                    "title": "Explain causes",
                    "description": "Research root causes",
                    "agent": "research",
                    "depends_on": ["task-1"],
                },
            ],
            "success_criteria": ["three products named"],
            "requires_approval": False,
        }
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "calls": 3,
            "model": "mock-1",
            "provider": "mock",
        }
        update_run(
            db_session,
            run["id"],
            status="completed",
            plan_json=plan,
            token_usage_json=usage,
            final_report="# Report\n\nDone.",
        )
        response = client.get(f"/api/runs/{run['id']}", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["plan"]["objective"] == DEMO_OBJECTIVE
        assert [task["id"] for task in body["plan"]["tasks"]] == ["task-1", "task-2"]
        assert body["token_usage"]["total_tokens"] == 150
        assert body["token_usage"]["calls"] == 3
        assert body["final_report"].startswith("# Report")

    def test_get_run_unknown_is_404(self, client, auth_headers):
        response = client.get("/api/runs/does-not-exist", headers=auth_headers)
        assert response.status_code == 404

    def test_run_tasks(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        add_task(
            db_session,
            run["id"],
            "task-1",
            title="Rank products",
            status="completed",
            attempts=1,
            result={"task_id": "task-1", "summary": "bottom three found"},
            evaluation={"task_id": "task-1", "score": 0.9, "passed": True},
        )
        add_task(
            db_session,
            run["id"],
            "task-2",
            title="Explain causes",
            agent="research",
            status="completed",
            attempts=2,
            result={"task_id": "task-2", "summary": "causes found"},
            evaluation={"task_id": "task-2", "score": 0.8, "passed": True},
        )
        response = client.get(f"/api/runs/{run['id']}/tasks", headers=auth_headers)
        assert response.status_code == 200
        tasks = response.json()
        assert len(tasks) == 2
        by_task = {task["task_id"]: task for task in tasks}
        assert by_task["task-1"]["agent"] == "analyst"
        assert by_task["task-2"]["agent"] == "research"
        assert by_task["task-2"]["attempts"] == 2
        assert by_task["task-1"]["evaluation"]["score"] == pytest.approx(0.9)
        assert by_task["task-1"]["result"]["summary"] == "bottom three found"
        assert by_task["task-1"]["status"] == "completed"

    def test_run_tasks_unknown_run_is_404(self, client, auth_headers):
        response = client.get("/api/runs/nope/tasks", headers=auth_headers)
        assert response.status_code == 404

    def test_run_report_and_artifact_list(
        self, client, auth_headers, stub_workflow, db_session
    ):
        run = create_run(client, auth_headers)
        update_run(db_session, run["id"], status="completed", final_report="# Final report")
        add_artifact(db_session, run["id"], name="report.md", content="# Final report")
        response = client.get(f"/api/runs/{run['id']}/report", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == run["id"]
        assert body["report"] == "# Final report"
        assert len(body["artifacts"]) == 1
        assert body["artifacts"][0]["name"] == "report.md"

        listing = client.get(f"/api/runs/{run['id']}/artifacts", headers=auth_headers)
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert len(items) == 1
        assert items[0]["kind"] == "markdown"

    def test_report_unknown_run_is_404(self, client, auth_headers):
        assert client.get("/api/runs/nope/report", headers=auth_headers).status_code == 404
        assert client.get("/api/runs/nope/artifacts", headers=auth_headers).status_code == 404

    def test_artifact_download_from_stored_content(
        self, client, auth_headers, stub_workflow, db_session
    ):
        run = create_run(client, auth_headers)
        artifact = add_artifact(
            db_session, run["id"], name="summary.md", content="# Summary\n\nAll good."
        )
        response = client.get(f"/api/artifacts/{artifact.id}/download", headers=auth_headers)
        assert response.status_code == 200
        assert response.text == "# Summary\n\nAll good."
        assert "text/markdown" in response.headers.get("content-type", "")
        assert "attachment" in response.headers.get("content-disposition", "")

    def test_artifact_download_from_file(
        self, client, auth_headers, stub_workflow, db_session, settings
    ):
        run = create_run(client, auth_headers)
        target = settings.artifacts_path / f"artifact-{uuid.uuid4().hex[:8]}.md"
        target.write_text("# File artifact\n\nPersisted on disk.", encoding="utf-8")
        artifact = add_artifact(db_session, run["id"], name="file.md", path=str(target))
        response = client.get(f"/api/artifacts/{artifact.id}/download", headers=auth_headers)
        assert response.status_code == 200
        assert "Persisted on disk." in response.text

    def test_artifact_download_requires_auth_and_ownership(self, client, auth_headers, db_session):
        run = create_run(client, auth_headers)
        artifact = add_artifact(db_session, run["id"], name="x.md", content="secret")
        assert client.get(f"/api/artifacts/{artifact.id}/download").status_code == 401
        other = register(client)
        response = client.get(
            f"/api/artifacts/{artifact.id}/download", headers=_bearer(other)
        )
        assert response.status_code == 404
        assert client.get(
            "/api/artifacts/does-not-exist/download", headers=auth_headers
        ).status_code == 404

    def test_artifact_without_content_or_file_is_404(
        self, client, auth_headers, stub_workflow, db_session
    ):
        run = create_run(client, auth_headers)
        artifact = add_artifact(db_session, run["id"], name="ghost.md", path="")
        response = client.get(f"/api/artifacts/{artifact.id}/download", headers=auth_headers)
        assert response.status_code == 404
        assert "downloadable content" in response.json()["detail"]


# ===========================================================================
# Events: durable log + SSE
# ===========================================================================
class TestEvents:
    def test_events_ordered_and_after_seq(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        add_event(db_session, run["id"], 1, "run.created", message="created")
        add_event(db_session, run["id"], 2, "plan.created", message="planned")
        add_event(db_session, run["id"], 3, "task.started", task_id="task-1", message="started")

        response = client.get(f"/api/runs/{run['id']}/events", headers=auth_headers)
        assert response.status_code == 200
        events = response.json()
        assert [event["seq"] for event in events] == [1, 2, 3]
        assert events[0]["type"] == "run.created"
        assert events[2]["task_id"] == "task-1"
        assert events[0]["run_id"] == run["id"]

        after = client.get(f"/api/runs/{run['id']}/events?after_seq=2", headers=auth_headers)
        assert after.status_code == 200
        assert [event["seq"] for event in after.json()] == [3]

    def test_events_unknown_run_is_404(self, client, auth_headers):
        assert client.get("/api/runs/nope/events", headers=auth_headers).status_code == 404

    def test_sse_requires_auth(self, client, auth_headers, stub_workflow):
        run = create_run(client, auth_headers)
        url = f"/api/runs/{run['id']}/events/stream"
        assert client.get(url).status_code == 401
        assert (
            client.get(url, headers={"Authorization": "Bearer invalid"}).status_code == 401
        )

    def test_sse_unknown_run_is_404(self, client, auth_headers):
        response = client.get("/api/runs/nope/events/stream", headers=auth_headers)
        assert response.status_code == 404

    def test_sse_replays_persisted_events_for_terminal_run(
        self, client, auth_headers, stub_workflow, db_session
    ):
        run = create_run(client, auth_headers)
        add_event(db_session, run["id"], 1, "run.started", message="started")
        add_event(db_session, run["id"], 2, "run.completed", message="done", data={"ok": True})
        update_run(db_session, run["id"], status="completed", completed_at=utcnow())

        with client.stream(
            "GET", f"/api/runs/{run['id']}/events/stream", headers=auth_headers
        ) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")
            body = "".join(response.iter_text())

        events = parse_sse_frames(body)
        assert [event["seq"] for event in events] == [1, 2]
        assert events[0]["type"] == "run.started"
        assert events[1]["type"] == "run.completed"
        assert events[1]["run_id"] == run["id"]
        assert "event: message" in body

    def test_sse_respects_after_seq(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        add_event(db_session, run["id"], 1, "run.started")
        add_event(db_session, run["id"], 2, "run.completed")
        update_run(db_session, run["id"], status="completed", completed_at=utcnow())

        with client.stream(
            "GET",
            f"/api/runs/{run['id']}/events/stream?after_seq=1",
            headers=auth_headers,
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())
        events = parse_sse_frames(body)
        assert [event["seq"] for event in events] == [2]


# ===========================================================================
# Cancel + approvals
# ===========================================================================
class TestCancelAndApprovals:
    def test_cancel_run(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        response = client.post(f"/api/runs/{run['id']}/cancel", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "cancelled"
        assert body["completed_at"] is not None
        assert stub_workflow.cancelled == [run["id"]]
        assert refresh_run(db_session, run["id"]).status == "cancelled"

    def test_cancel_terminal_run_is_idempotent(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        update_run(db_session, run["id"], status="completed", completed_at=utcnow())
        response = client.post(f"/api/runs/{run['id']}/cancel", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert stub_workflow.cancelled == []

    def test_cancel_unknown_run_is_404(self, client, auth_headers):
        assert client.post("/api/runs/nope/cancel", headers=auth_headers).status_code == 404

    def test_pending_approval_surfaces_on_run(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        update_run(db_session, run["id"], status="awaiting_approval")
        approval = add_approval(
            db_session,
            run["id"],
            title="Apply pricing actions",
            requested_action="Apply a 5% discount",
            reason="Sensitive execution",
        )
        response = client.get(f"/api/runs/{run['id']}", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "awaiting_approval"
        assert body["pending_approval"] == {
            "id": approval.id,
            "task_id": "task-2",
            "title": "Apply pricing actions",
            "requested_action": "Apply a 5% discount",
            "reason": "Sensitive execution",
        }

    def test_approve_resumes_workflow(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        update_run(db_session, run["id"], status="awaiting_approval")
        approval = add_approval(db_session, run["id"])
        response = client.post(
            f"/api/runs/{run['id']}/approvals/{approval.id}",
            json={"decision": "approved", "note": "go ahead"},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["pending_approval"] is None
        assert stub_workflow.resumed == [run["id"]]

        db_session.expire_all()
        row = db_session.get(Approval, approval.id)
        assert row.status == "approved"
        assert row.note == "go ahead"
        assert row.decided_by is not None
        assert row.decided_at is not None

    def test_reject_does_not_resume_as_approved(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        update_run(db_session, run["id"], status="awaiting_approval")
        approval = add_approval(db_session, run["id"])
        response = client.post(
            f"/api/runs/{run['id']}/approvals/{approval.id}",
            json={"decision": "rejected"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        db_session.expire_all()
        row = db_session.get(Approval, approval.id)
        assert row.status == "rejected"
        # The API hands the decision to the service; it never marks the approval approved.
        assert stub_workflow.resumed == [run["id"]]

    def test_approval_invalid_cases(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        update_run(db_session, run["id"], status="awaiting_approval")
        approval = add_approval(db_session, run["id"])

        base = f"/api/runs/{run['id']}/approvals"
        # Unknown approval id.
        assert (
            client.post(
                f"{base}/missing", json={"decision": "approved"}, headers=auth_headers
            ).status_code
            == 404
        )
        # Approval from a different run.
        other_run = create_run(client, auth_headers, SECOND_OBJECTIVE)
        assert (
            client.post(
                f"/api/runs/{other_run['id']}/approvals/{approval.id}",
                json={"decision": "approved"},
                headers=auth_headers,
            ).status_code
            == 404
        )
        # Invalid decision payloads.
        for payload in ({"decision": "maybe"}, {}, {"decision": ""}):
            assert (
                client.post(
                    f"{base}/{approval.id}", json=payload, headers=auth_headers
                ).status_code
                == 422
            )
        # Already decided.
        assert (
            client.post(
                f"{base}/{approval.id}",
                json={"decision": "approved", "note": "first"},
                headers=auth_headers,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/{approval.id}", json={"decision": "rejected"}, headers=auth_headers
            ).status_code
            == 409
        )
        # Run that is already terminal cannot accept approvals.
        finished = create_run(client, auth_headers, SECOND_OBJECTIVE)
        update_run(db_session, finished["id"], status="completed", completed_at=utcnow())
        finished_approval = add_approval(db_session, finished["id"], status="pending")
        response = client.post(
            f"/api/runs/{finished['id']}/approvals/{finished_approval.id}",
            json={"decision": "approved"},
            headers=auth_headers,
        )
        assert response.status_code == 409

    def test_approval_requires_auth(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        update_run(db_session, run["id"], status="awaiting_approval")
        approval = add_approval(db_session, run["id"])
        response = client.post(
            f"/api/runs/{run['id']}/approvals/{approval.id}", json={"decision": "approved"}
        )
        assert response.status_code == 401

    def test_resume_failure_marks_run_failed(self, client, auth_headers, stub_workflow, db_session):
        run = create_run(client, auth_headers)
        update_run(db_session, run["id"], status="awaiting_approval")
        approval = add_approval(db_session, run["id"])
        stub_workflow.fail_resume = RuntimeError("cannot resume")
        response = client.post(
            f"/api/runs/{run['id']}/approvals/{approval.id}",
            json={"decision": "approved"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "failed"
        assert "failed to resume workflow" in response.json()["error"]


# ===========================================================================
# Metrics
# ===========================================================================
class TestMetrics:
    def test_metrics_shape_and_aggregation(
        self, client, auth_headers, stub_workflow, db_session
    ):
        completed = create_run(client, auth_headers)
        failed = create_run(client, auth_headers, SECOND_OBJECTIVE)
        started = datetime.now(timezone.utc)
        update_run(
            db_session,
            completed["id"],
            status="completed",
            started_at=started,
            completed_at=started + timedelta(seconds=12),
            token_usage_json={"total_tokens": 120},
        )
        update_run(
            db_session,
            failed["id"],
            status="failed",
            started_at=started,
            completed_at=started + timedelta(seconds=4),
            token_usage_json={"total_tokens": 30},
            error="boom",
        )
        add_task(db_session, completed["id"], "task-1", status="completed", attempts=2)
        db_session.add(
            EvaluationMetric(
                run_id=completed["id"],
                task_id="task-1",
                agent="analyst",
                score=0.8,
                passed=True,
                attempts=2,
                latency_ms=100,
            )
        )
        db_session.commit()

        response = client.get("/api/metrics/runs", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total_runs"] == 2
        assert body["completed"] == 1
        assert body["failed"] == 1
        assert body["success_rate"] == pytest.approx(0.5)
        assert body["total_tokens"] == 150
        assert body["total_retries"] == 1
        assert body["avg_eval_score"] == pytest.approx(0.8)
        assert body["avg_duration_seconds"] == pytest.approx(8.0)
        assert body["by_status"]["completed"] == 1
        assert body["by_status"]["failed"] == 1
        assert body["by_status"]["queued"] == 0

    def test_metrics_isolated_for_members(self, client, auth_headers, stub_workflow):
        create_run(client, auth_headers)
        other = register(client)
        response = client.get("/api/metrics/runs", headers=_bearer(other))
        assert response.status_code == 200
        body = response.json()
        assert body["total_runs"] == 0
        assert body["success_rate"] == 0.0
        assert body["avg_eval_score"] is None
        assert body["avg_duration_seconds"] is None

    def test_metrics_requires_auth(self, client):
        assert client.get("/api/metrics/runs").status_code == 401


# ===========================================================================
# Agents / tools catalogs
# ===========================================================================
class TestCatalogs:
    def test_tools_endpoint_lists_registry(self, client):
        response = client.get("/api/tools")
        assert response.status_code == 200
        tools = response.json()["tools"]
        assert isinstance(tools, list) and tools
        names = {tool["name"] for tool in tools}
        expected = {"sales_sql", "csv_query", "knowledge_search", "web_search", "report_writer"}
        assert expected <= names, f"missing tools from registry: {sorted(expected - names)}"
        for tool in tools:
            assert tool["name"]
            assert tool["description"]
            assert isinstance(tool["scopes"], list)
            assert isinstance(tool["sensitive"], bool)
            assert isinstance(tool["input_schema"], dict)
            assert tool["category"]

    def test_agents_endpoint_lists_roles(self, client):
        response = client.get("/api/agents")
        assert response.status_code == 200
        agents = response.json()["agents"]
        names = {agent["name"] for agent in agents}
        assert {"research", "analyst", "execution", "reporting"} <= names
        for agent in agents:
            assert agent["role"] in {"research", "analyst", "execution", "reporting"}
            assert agent["description"]
            assert isinstance(agent["capabilities"], list)
            assert isinstance(agent["tools"], list)


# ===========================================================================
# Knowledge ingest + search
# ===========================================================================
class TestKnowledge:
    def test_ingest_then_search(self, client):
        text = (
            "# Zephyr Chair Warranty Policy\n\n"
            "The Zephyr ergonomic chair carries a 36-month warranty covering the "
            "gas lift and the lumbar mechanism. Defects reported within the first "
            "90 days are replaced free of charge under the quality escalation "
            "process. Out-of-warranty repairs are quoted per supplier agreements."
        )
        ingest = client.post(
            "/api/knowledge/ingest",
            json={"title": "Zephyr Chair Warranty Policy", "source": "qa-test", "text": text},
        )
        assert ingest.status_code == 200, ingest.text
        body = ingest.json()
        assert body["chunks_added"] >= 1

        search = client.get(
            "/api/knowledge/search", params={"q": "Zephyr chair warranty gas lift", "k": 3}
        )
        assert search.status_code == 200, search.text
        results = search.json()["results"]
        assert results, "search returned no results after ingest"
        assert results[0]["title"] == "Zephyr Chair Warranty Policy"
        assert results[0]["source"] == "qa-test"
        assert results[0]["text"]
        assert 0.0 <= results[0]["score"] <= 1.0

    def test_ingest_validation(self, client):
        response = client.post(
            "/api/knowledge/ingest", json={"title": "short", "text": "too short"}
        )
        assert response.status_code == 422

    def test_search_validation(self, client):
        assert client.get("/api/knowledge/search").status_code == 422
        assert client.get("/api/knowledge/search", params={"q": ""}).status_code == 422


# ===========================================================================
# Authentication / ownership matrix
# ===========================================================================
PROTECTED_ENDPOINTS = [
    ("GET", "/api/auth/me"),
    ("GET", "/api/runs"),
    ("POST", "/api/runs"),
    ("GET", "/api/runs/{run_id}"),
    ("GET", "/api/runs/{run_id}/tasks"),
    ("GET", "/api/runs/{run_id}/events"),
    ("GET", "/api/runs/{run_id}/events/stream"),
    ("GET", "/api/runs/{run_id}/report"),
    ("GET", "/api/runs/{run_id}/artifacts"),
    ("POST", "/api/runs/{run_id}/cancel"),
    ("GET", "/api/metrics/runs"),
    ("GET", "/api/artifacts/{artifact_id}/download"),
]


class TestAuthAndOwnership:
    @pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
    def test_protected_endpoints_require_token(self, client, method, path):
        url = path.format(run_id="any-run", artifact_id="any-artifact")
        response = client.request(method, url, json={} if method == "POST" else None)
        assert response.status_code == 401, f"{method} {url} -> {response.status_code}"

    def test_cross_tenant_run_is_404_even_with_valid_token(self, client, stub_workflow):
        owner = register(client)
        run = create_run(client, _bearer(owner))
        intruder = register(client)
        headers = _bearer(intruder)
        for path in (
            f"/api/runs/{run['id']}",
            f"/api/runs/{run['id']}/tasks",
            f"/api/runs/{run['id']}/events",
            f"/api/runs/{run['id']}/report",
            f"/api/runs/{run['id']}/artifacts",
        ):
            assert client.get(path, headers=headers).status_code == 404, path
        assert client.post(f"/api/runs/{run['id']}/cancel", headers=headers).status_code == 404
        assert (
            client.get(f"/api/runs/{run['id']}/events/stream", headers=headers).status_code
            == 404
        )

    def test_cross_tenant_list_isolation(self, client, stub_workflow):
        owner = register(client)
        run = create_run(client, _bearer(owner))
        intruder = register(client)
        listing = client.get("/api/runs", headers=_bearer(intruder)).json()
        assert listing["total"] == 0
        assert listing["items"] == []
        # The owner sees exactly their own run.
        owner_listing = client.get("/api/runs", headers=_bearer(owner)).json()
        assert owner_listing["total"] == 1
        assert owner_listing["items"][0]["id"] == run["id"]

    def test_admin_can_read_any_run(self, client, auth_headers, stub_workflow):
        member = register(client)
        run = create_run(client, _bearer(member))
        response = client.get(f"/api/runs/{run['id']}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["id"] == run["id"]
        listing = client.get("/api/runs", headers=auth_headers).json()
        assert listing["total"] >= 1

    def test_cross_tenant_approval_decision_is_404(self, client, stub_workflow, db_session):
        owner = register(client)
        run = create_run(client, _bearer(owner))
        update_run(db_session, run["id"], status="awaiting_approval")
        approval = add_approval(db_session, run["id"])
        intruder = register(client)
        response = client.post(
            f"/api/runs/{run['id']}/approvals/{approval.id}",
            json={"decision": "approved"},
            headers=_bearer(intruder),
        )
        assert response.status_code == 404
