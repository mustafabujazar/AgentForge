"""FastAPI smoke tests for the AgentForge HTTP API.

This suite is self-contained, offline and fast (<20s).  It relies on the shared
``tests/conftest.py`` isolation harness (session temp SQLite DB, mock LLM,
per-test schema reset, lifespan ``TestClient``), and replaces the orchestrator
with an async stub so the API contract is exercised without running a real
multi-agent workflow (that path is covered by the orchestrator/QA suites).
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

DEMO_EMAIL = "demo@agentforge.io"
DEMO_PASSWORD = "demo1234"
DEMO_OBJECTIVE = (
    "Analyze sales performance and identify the three worst products, "
    "investigate the causes and write a report with recommendations."
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
class StubWorkflowService:
    """Async stand-in for ``app.orchestrator.service.workflow_service``."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.resumed: list[str] = []
        self.cancelled: list[str] = []

    async def start_run(self, run_id: str, objective: str) -> None:
        self.started.append((run_id, objective))

    async def resume_run(self, run_id: str) -> None:
        self.resumed.append(run_id)

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


@pytest.fixture()
def workflow_stub(monkeypatch):
    """Replace the lazily-resolved orchestrator singleton with a recording stub."""
    from app.api import runs as runs_module

    stub = StubWorkflowService()
    monkeypatch.setattr(runs_module, "workflow_service", stub)
    return stub


def _db_session():
    from app.db.base import get_session_factory

    return get_session_factory()()


def _register(
    client: TestClient,
    *,
    email: str | None = None,
    password: str = "smoke-pass-1234",
    full_name: str = "Smoke Tester",
) -> tuple[dict, dict[str, str], str]:
    email = email or f"smoke.{uuid.uuid4().hex[:12]}@example.com"
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "full_name": full_name},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return payload, {"Authorization": f"Bearer {payload['access_token']}"}, email


def _create_run_via_api(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/runs", json={"objective": DEMO_OBJECTIVE}, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def _set_run_status(run_id: str, status: str) -> None:
    from app.db.models import Run

    session = _db_session()
    try:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = status
        session.add(run)
        session.commit()
    finally:
        session.close()


def _insert_events(run_id: str, *, count: int = 3, run_status: str | None = None) -> None:
    from app.db.models import Run, RunEvent

    session = _db_session()
    try:
        run = session.get(Run, run_id)
        assert run is not None
        for seq in range(1, count + 1):
            session.add(
                RunEvent(
                    run_id=run_id,
                    seq=seq,
                    type="run.completed" if seq == count else "task.started",
                    agent="analyst",
                    task_id="task-1",
                    message=f"event {seq}",
                    data_json={"seq": seq},
                )
            )
        if run_status is not None:
            run.status = run_status
        session.add(run)
        session.commit()
    finally:
        session.close()


def _set_awaiting_approval(run_id: str) -> str:
    from app.db.models import Approval, Run

    session = _db_session()
    try:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = "awaiting_approval"
        approval = Approval(
            run_id=run_id,
            task_id="task-2",
            title="Execute approved action",
            requested_action="Apply retention discount to the top 20 accounts",
            reason="Sensitive write action requires human sign-off",
            risk_level="high",
            status="pending",
        )
        session.add(run)
        session.add(approval)
        session.commit()
        session.refresh(approval)
        return approval.id
    finally:
        session.close()


def _mark_run_completed(run_id: str, *, total_tokens: int = 120) -> None:
    from app.db.models import EvaluationMetric, Run, TaskRecord

    session = _db_session()
    try:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = "completed"
        run.started_at = run.created_at
        run.completed_at = run.created_at + timedelta(seconds=42)
        run.token_usage_json = {
            "prompt_tokens": 70,
            "completion_tokens": 50,
            "total_tokens": total_tokens,
            "calls": 2,
        }
        session.add(
            TaskRecord(
                run_id=run_id,
                task_id="task-1",
                title="Analyze sales",
                agent="analyst",
                status="completed",
                attempts=2,
            )
        )
        session.add(
            EvaluationMetric(
                run_id=run_id,
                task_id="task-1",
                agent="analyst",
                score=0.8,
                passed=True,
                attempts=2,
            )
        )
        session.commit()
    finally:
        session.close()


def _insert_artifact(run_id: str) -> str:
    from app.db.models import Artifact

    session = _db_session()
    try:
        artifact = Artifact(
            run_id=run_id,
            name="report.md",
            kind="markdown",
            path="",
            content="# AgentForge Report\n\nThree products underperform.\n",
            description="Smoke-test report",
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact.id
    finally:
        session.close()


def _frame_payload(frame: str) -> dict:
    """Parse the ``data:`` JSON payload out of a single SSE frame."""
    for line in frame.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"SSE frame without a data payload: {frame!r}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["llm_provider"] == "mock"
    assert body["database"] == "sqlite"
    assert body["environment"]


def test_api_root(client: TestClient) -> None:
    response = client.get("/api")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "AgentForge"
    assert body["status"] == "ok"
    assert body["docs"] == "/docs"


def test_lifespan_seeds_demo_admin(client: TestClient) -> None:
    login = client.post("/api/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert login.status_code == 200, login.text
    payload = login.json()
    assert payload["user"]["email"] == DEMO_EMAIL
    assert payload["user"]["role"] == "admin"
    headers = {"Authorization": f"Bearer {payload['access_token']}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "admin"


def test_auth_register_login_me(client: TestClient) -> None:
    payload, headers, email = _register(client, full_name="Ada Lovelace")
    assert payload["token_type"] == "bearer"
    assert payload["user"]["email"] == email
    assert payload["user"]["role"] == "member"

    duplicate = client.post(
        "/api/auth/register",
        json={"email": email, "password": "smoke-pass-1234", "full_name": "Dup"},
    )
    assert duplicate.status_code == 409

    assert (
        client.post("/api/auth/login", json={"email": email, "password": "wrong-pass"}).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login", json={"email": "nobody@example.com", "password": "whatever1"}
        ).status_code
        == 401
    )

    login = client.post(
        "/api/auth/login", json={"email": email, "password": "smoke-pass-1234"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == email
    assert me.json()["full_name"] == "Ada Lovelace"
    assert me.json()["created_at"]

    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"}).status_code
        == 401
    )


def test_create_run_starts_workflow_and_lists(client: TestClient, workflow_stub) -> None:
    _, headers, _ = _register(client)
    response = client.post(
        "/api/runs",
        json={"objective": DEMO_OBJECTIVE, "auto_approve": False},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["id"]
    assert run["status"] == "queued"
    assert run["objective"] == DEMO_OBJECTIVE
    assert workflow_stub.started == [(run["id"], DEMO_OBJECTIVE)]

    detail = client.get(f"/api/runs/{run['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["id"] == run["id"]
    assert detail.json()["pending_approval"] is None

    listing = client.get("/api/runs", headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] >= 1
    assert run["id"] in {item["id"] for item in body["items"]}

    filtered = client.get("/api/runs", params={"status": "queued"}, headers=headers)
    assert filtered.status_code == 200
    assert all(item["status"] == "queued" for item in filtered.json()["items"])

    paged = client.get("/api/runs", params={"limit": 1, "offset": 0}, headers=headers)
    assert paged.status_code == 200
    assert len(paged.json()["items"]) <= 1

    assert client.get("/api/runs", params={"status": "nope"}, headers=headers).status_code == 422

    tasks = client.get(f"/api/runs/{run['id']}/tasks", headers=headers)
    assert tasks.status_code == 200
    assert tasks.json() == []

    report = client.get(f"/api/runs/{run['id']}/report", headers=headers)
    assert report.status_code == 200
    assert report.json()["run_id"] == run["id"]
    assert report.json()["report"] is None


def test_run_ownership_isolation(client: TestClient, workflow_stub) -> None:
    _, headers, _ = _register(client)
    _, other_headers, _ = _register(client)
    run_id = _create_run_via_api(client, headers)

    assert client.get(f"/api/runs/{run_id}", headers=other_headers).status_code == 404
    assert client.get(f"/api/runs/{run_id}/events", headers=other_headers).status_code == 404
    assert client.get(f"/api/runs/{run_id}/tasks", headers=other_headers).status_code == 404

    other_runs = client.get("/api/runs", headers=other_headers).json()
    assert run_id not in {item["id"] for item in other_runs["items"]}
    assert other_runs["total"] == 0

    assert client.get(f"/api/runs/{run_id}").status_code == 401
    assert client.get(f"/api/runs/{uuid.uuid4().hex}", headers=headers).status_code == 404


def test_events_endpoint_and_sse_replay(client: TestClient, workflow_stub) -> None:
    _, headers, _ = _register(client)
    run_id = _create_run_via_api(client, headers)
    _insert_events(run_id, count=3, run_status="completed")

    events = client.get(f"/api/runs/{run_id}/events", headers=headers)
    assert events.status_code == 200
    assert [event["seq"] for event in events.json()] == [1, 2, 3]
    assert events.json()[0]["type"] == "task.started"
    assert events.json()[-1]["type"] == "run.completed"

    after = client.get(f"/api/runs/{run_id}/events", params={"after_seq": 2}, headers=headers)
    assert [event["seq"] for event in after.json()] == [3]

    with client.stream(
        "GET",
        f"/api/runs/{run_id}/events/stream",
        params={"after_seq": 1},
        headers=headers,
        timeout=10,
    ) as stream:
        assert stream.status_code == 200, stream.read()
        assert "text/event-stream" in stream.headers["content-type"]
        body = "".join(stream.iter_text())

    frames = [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert [frame["seq"] for frame in frames] == [2, 3]
    assert all(frame["run_id"] == run_id for frame in frames)
    assert "event: message" in body

    assert client.get(f"/api/runs/{run_id}/events/stream").status_code == 401


def test_approval_decision_flow(client: TestClient, workflow_stub) -> None:
    _, headers, _ = _register(client)
    me = client.get("/api/auth/me", headers=headers).json()
    run_id = _create_run_via_api(client, headers)
    approval_id = _set_awaiting_approval(run_id)

    detail = client.get(f"/api/runs/{run_id}", headers=headers)
    assert detail.status_code == 200
    pending = detail.json()["pending_approval"]
    assert pending is not None
    assert pending["id"] == approval_id
    assert pending["task_id"] == "task-2"
    assert pending["requested_action"]

    response = client.post(
        f"/api/runs/{run_id}/approvals/{approval_id}",
        json={"decision": "approved", "note": "proceed with retention offer"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == run_id
    assert workflow_stub.resumed == [run_id]

    from app.db.models import Approval

    session = _db_session()
    try:
        approval = session.get(Approval, approval_id)
        assert approval is not None
        assert approval.status == "approved"
        assert approval.decided_by == me["id"]
        assert approval.note == "proceed with retention offer"
        assert approval.decided_at is not None
    finally:
        session.close()

    duplicate = client.post(
        f"/api/runs/{run_id}/approvals/{approval_id}",
        json={"decision": "rejected"},
        headers=headers,
    )
    assert duplicate.status_code == 409

    missing = client.post(
        f"/api/runs/{run_id}/approvals/{uuid.uuid4().hex}",
        json={"decision": "approved"},
        headers=headers,
    )
    assert missing.status_code == 404


def test_cancel_run(client: TestClient, workflow_stub) -> None:
    _, headers, _ = _register(client)
    run_id = _create_run_via_api(client, headers)
    _set_run_status(run_id, "running")

    response = client.post(f"/api/runs/{run_id}/cancel", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert workflow_stub.cancelled == [run_id]

    again = client.post(f"/api/runs/{run_id}/cancel", headers=headers)
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"


def test_run_tasks_and_report_payload(client: TestClient, workflow_stub) -> None:
    _, headers, _ = _register(client)
    run_id = _create_run_via_api(client, headers)

    from app.db.models import Artifact, Run, TaskRecord

    session = _db_session()
    try:
        run = session.get(Run, run_id)
        assert run is not None
        run.final_report = "# Final Report\n\nGrounded recommendations."
        session.add(run)
        session.add(
            TaskRecord(
                run_id=run_id,
                task_id="task-1",
                title="Analyze sales",
                agent="analyst",
                status="completed",
                attempts=2,
                result_json={"summary": "done"},
                evaluation_json={
                    "task_id": "task-1",
                    "score": 0.9,
                    "passed": True,
                    "feedback": "solid analysis",
                },
            )
        )
        session.add(
            Artifact(
                run_id=run_id,
                name="report.md",
                kind="markdown",
                path="",
                content="# Final Report",
                description="final",
            )
        )
        session.commit()
    finally:
        session.close()

    tasks = client.get(f"/api/runs/{run_id}/tasks", headers=headers)
    assert tasks.status_code == 200, tasks.text
    payload = tasks.json()
    assert len(payload) == 1
    task = payload[0]
    assert task["task_id"] == "task-1"
    assert task["agent"] == "analyst"
    assert task["status"] == "completed"
    assert task["attempts"] == 2
    assert task["result"] == {"summary": "done"}
    assert task["evaluation"]["score"] == 0.9

    report = client.get(f"/api/runs/{run_id}/report", headers=headers)
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["report"].startswith("# Final Report")
    assert len(body["artifacts"]) == 1
    assert body["artifacts"][0]["name"] == "report.md"


def test_create_run_graceful_when_orchestrator_missing(
    client: TestClient, monkeypatch
) -> None:
    from app.api import runs as runs_module

    def _unavailable():
        raise ModuleNotFoundError("No module named 'app.orchestrator.service'")

    monkeypatch.setattr(runs_module, "_get_workflow_service", _unavailable)
    _, headers, _ = _register(client)
    response = client.post("/api/runs", json={"objective": DEMO_OBJECTIVE}, headers=headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["id"]
    assert body["status"] == "queued"


def test_sse_streams_live_broker_events_then_closes(
    client: TestClient, workflow_stub
) -> None:
    import asyncio
    from datetime import datetime, timezone

    from app.api.runs import _event_stream
    from app.core.events import broker
    from app.db.models import RunEvent

    _, headers, _ = _register(client)
    run_id = _create_run_via_api(client, headers)
    _set_run_status(run_id, "running")

    async def scenario() -> list[dict]:
        broker.bind_loop()  # the app lifespan normally does this at startup
        stream = _event_stream(run_id, 0)
        first = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0.15)  # let the generator replay history + subscribe
        live_event = {
            "seq": 1,
            "run_id": run_id,
            "type": "task.started",
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": "analyst",
            "task_id": "task-1",
            "message": "live event",
            "data": {"tool": "sales_sql"},
            "duration_ms": 3,
        }
        broker.publish_threadsafe(run_id, live_event)
        collected = [_frame_payload(await asyncio.wait_for(first, timeout=10))]

        # Make the run terminal and publish the final event; the stream must flush
        # it and then close on its own.
        _set_run_status(run_id, "completed")
        session = _db_session()
        try:
            session.add(
                RunEvent(
                    run_id=run_id,
                    seq=2,
                    type="run.completed",
                    message="finished",
                    data_json={},
                )
            )
            session.commit()
        finally:
            session.close()
        broker.publish_threadsafe(
            run_id,
            {
                **live_event,
                "seq": 2,
                "type": "run.completed",
                "message": "finished",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )

        try:
            while True:
                collected.append(
                    _frame_payload(await asyncio.wait_for(stream.__anext__(), timeout=10))
                )
        except StopAsyncIteration:
            pass
        return collected

    frames = asyncio.run(scenario())
    assert [frame["seq"] for frame in frames] == [1, 2]
    assert frames[0]["type"] == "task.started"
    assert frames[0]["message"] == "live event"
    assert frames[1]["type"] == "run.completed"


def test_agents_and_tools_endpoints(client: TestClient) -> None:
    agents_response = client.get("/api/agents")
    assert agents_response.status_code == 200
    agents = agents_response.json()["agents"]
    names = {agent["name"] for agent in agents}
    assert {"research", "analyst", "execution", "reporting"} <= names
    for agent in agents:
        assert set(agent) == {"name", "role", "description", "capabilities", "tools"}
        assert isinstance(agent["capabilities"], list)
        assert isinstance(agent["tools"], list)

    tools_response = client.get("/api/tools")
    assert tools_response.status_code == 200
    tools = tools_response.json()["tools"]
    assert isinstance(tools, list)
    for tool in tools:
        assert {"name", "description", "category", "sensitive", "scopes", "input_schema"} <= set(
            tool
        )


def test_run_metrics(client: TestClient, workflow_stub) -> None:
    _, headers, _ = _register(client)
    run_id = _create_run_via_api(client, headers)
    _mark_run_completed(run_id, total_tokens=120)

    response = client.get("/api/metrics/runs", headers=headers)
    assert response.status_code == 200, response.text
    metrics = response.json()
    assert metrics["total_runs"] >= 1
    assert metrics["completed"] >= 1
    assert metrics["failed"] >= 0
    assert 0.0 <= metrics["success_rate"] <= 1.0
    assert metrics["total_tokens"] >= 120
    assert metrics["total_retries"] >= 1
    assert metrics["avg_eval_score"] is not None
    assert metrics["avg_duration_seconds"] is not None
    assert isinstance(metrics["by_status"], dict)
    assert metrics["by_status"]["completed"] >= 1

    assert client.get("/api/metrics/runs").status_code == 401


def test_artifacts_list_and_download(client: TestClient, workflow_stub) -> None:
    _, headers, _ = _register(client)
    _, other_headers, _ = _register(client)
    run_id = _create_run_via_api(client, headers)
    artifact_id = _insert_artifact(run_id)

    listing = client.get(f"/api/runs/{run_id}/artifacts", headers=headers)
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == artifact_id
    assert items[0]["kind"] == "markdown"
    assert items[0]["run_id"] == run_id

    download = client.get(f"/api/artifacts/{artifact_id}/download", headers=headers)
    assert download.status_code == 200, download.text
    assert "text/markdown" in download.headers["content-type"]
    assert "# AgentForge Report" in download.text

    assert (
        client.get(f"/api/artifacts/{artifact_id}/download", headers=other_headers).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/artifacts/{uuid.uuid4().hex}/download", headers=headers
        ).status_code
        == 404
    )


def test_knowledge_endpoints_tolerant_to_rag_status(client: TestClient) -> None:
    search = client.get("/api/knowledge/search", params={"q": "pricing discount policy", "k": 3})
    if search.status_code == 503:
        pytest.skip("RAG layer is not available yet (parallel work in progress)")
    assert search.status_code == 200, search.text
    assert isinstance(search.json()["results"], list)

    ingest = client.post(
        "/api/knowledge/ingest",
        json={
            "title": "Smoke test memo",
            "source": "pytest",
            "text": "AgentForge smoke-test knowledge document about pricing, discounts and returns.",
        },
    )
    assert ingest.status_code in (200, 503), ingest.text
    if ingest.status_code == 200:
        assert ingest.json()["chunks_added"] >= 1
