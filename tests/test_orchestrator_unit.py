"""Offline unit tests for the orchestrator + agents (no network, no real provider).

Coverage:
1. Plan → task loop → evaluation → retry (analyst attempt 1 fails rules, attempt 2
   passes) → synthesis → persisted final report + artifact + event log.
2. HITL: a sensitive objective pauses at ``awaiting_approval``; approving resumes
   from the paused task; rejecting skips it and still delivers a report.
3. Graceful degradation when the per-run LLM budget is exhausted (heuristic plan,
   template report, run still terminal + honest).

The LLM is a scripted in-test :class:`BaseLLM`; the tool registry is a local stub
registry so tools-rag modules are not a test dependency.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import configure_database, init_db, session_scope
from app.db.models import Approval, Artifact, Run, RunEvent, TaskRecord
from app.llm.base import BaseLLM, ChatMessage, LLMResponse, TokenUsage
from app.orchestrator.persistence import latest_approval
from app.orchestrator.service import WorkflowService
from app.schemas.workflow import AgentRole
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolSpec
from app.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Stub tools (deliberately not the real tools-rag implementations)
# ---------------------------------------------------------------------------
class StubSalesSql(BaseTool):
    spec = ToolSpec(
        name="sales_sql",
        description="Run a read-only SQL query against the demo sales database.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        scopes=("read:sales",),
        category="data",
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "")
        return ToolResult.success(
            {
                "query": query,
                "columns": ["product", "revenue", "return_rate"],
                "rows": [
                    {"product": "Tide Portable Speaker", "revenue": 138612.78, "return_rate": 0.11},
                    {"product": "Cirrus Air Purifier", "revenue": 337551.94, "return_rate": 0.018},
                    {"product": "Solstice Ergonomic Chair", "revenue": 383761.96, "return_rate": 0.127},
                ],
                "row_count": 3,
            }
        )


class StubKnowledgeSearch(BaseTool):
    spec = ToolSpec(
        name="knowledge_search",
        description="Search internal knowledge memos and return cited passages.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
            "required": ["query"],
        },
        scopes=("read:knowledge",),
        category="knowledge",
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.success(
            {
                "query": str(args.get("query") or ""),
                "results": [
                    {
                        "title": "Quality Escalation Process",
                        "source": "quality_escalation.md",
                        "text": (
                            "Repeated returns on Tide speakers trace to a supplier QC regression; "
                            "quality escalations take a 14-day median."
                        ),
                        "score": 0.91,
                    },
                    {
                        "title": "Pricing Policy",
                        "source": "pricing_policy.md",
                        "text": (
                            "Pricing policy caps discretionary discounts at 20%; chairs average "
                            "31% discounts, eroding gross margin."
                        ),
                        "score": 0.84,
                    },
                ],
                "count": 2,
            }
        )


class StubWebSearch(BaseTool):
    spec = ToolSpec(
        name="web_search",
        description="Offline market-note search.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        scopes=("read:web",),
        category="web",
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.success({"query": args.get("query"), "results": [], "count": 0})


class StubCsvQuery(BaseTool):
    spec = ToolSpec(
        name="csv_query",
        description="Aggregate a demo CSV.",
        input_schema={
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
        },
        scopes=("read:sales",),
        category="data",
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.success({"file": args.get("file"), "rows": [], "row_count": 0})


class StubReportWriter(BaseTool):
    spec = ToolSpec(
        name="report_writer",
        description="Write a Markdown artifact under the run's artifacts directory.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["title", "content"],
        },
        scopes=("write:artifacts",),
        category="artifact",
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        title = str(args.get("title") or "report")
        content = str(args.get("content") or "")
        root = ctx.resolved_artifacts_dir
        path = root / f"stub-{title[:24].replace(' ', '-').lower()}.md"
        path.write_text(content, encoding="utf-8")
        return ToolResult.success(
            {
                "path": str(path),
                "name": path.name,
                "title": title,
                "kind": "markdown",
                "chars": len(content),
                "description": "stub artifact",
            }
        )


def build_stub_registry() -> ToolRegistry:
    registry = ToolRegistry()
    # Do not lazily auto-import the real tools-rag modules in unit tests.
    registry._defaults_loaded = True  # type: ignore[attr-defined]
    for tool in (
        StubSalesSql(),
        StubKnowledgeSearch(),
        StubWebSearch(),
        StubCsvQuery(),
        StubReportWriter(),
    ):
        registry.register(tool)
    return registry


# ---------------------------------------------------------------------------
# Scripted fake LLM
# ---------------------------------------------------------------------------
DEFAULT_PLAN = {
    "objective": "",
    "summary": "Research, analyse, plan remediation, report.",
    "tasks": [
        {
            "id": "task-1",
            "title": "Investigate root causes with internal memos",
            "description": "Find why products underperform using internal knowledge.",
            "agent": "research",
            "depends_on": [],
            "tool_hints": ["knowledge_search"],
            "sensitive": False,
            "expected_output": "3 findings with sources",
            "success_criteria": ["At least 3 findings with evidence and sources"],
        },
        {
            "id": "task-2",
            "title": "Quantify product performance",
            "description": "Trailing-12m revenue, returns, discounts by product.",
            "agent": "analyst",
            "depends_on": ["task-1"],
            "tool_hints": ["sales_sql"],
            "sensitive": False,
            "expected_output": "Ranked products with metrics",
            "success_criteria": ["Trailing-12m revenue by product computed with SQL"],
        },
        {
            "id": "task-3",
            "title": "Draft the remediation action plan",
            "description": "Prioritized actions with owners and expected impact.",
            "agent": "execution",
            "depends_on": ["task-2"],
            "tool_hints": ["report_writer"],
            "sensitive": False,
            "expected_output": "Action plan",
            "success_criteria": ["Actions are specific and owned"],
        },
        {
            "id": "task-4",
            "title": "Compose the final business report",
            "description": "Executive Markdown report with evidence and recommendations.",
            "agent": "reporting",
            "depends_on": ["task-2", "task-3"],
            "tool_hints": ["report_writer"],
            "sensitive": False,
            "expected_output": "Markdown report",
            "success_criteria": ["Complete Markdown report"],
        },
    ],
    "success_criteria": ["Identify the worst products", "Explain causes", "Deliver a report"],
    "requires_approval": False,
}

REPORT_MARKDOWN = """# Business Performance Analysis: Sales Underperformance

## Executive Summary

Three products account for the majority of underperformance: Tide Portable Speaker,
Cirrus Air Purifier and Solstice Ergonomic Chair. Trailing-12-month revenue is
$138,612.78, $337,551.94 and $383,761.96 respectively, with Tide showing both the
lowest revenue and an elevated return rate of 11%.

## Key Findings & Evidence

- Tide revenue collapsed to $138,612.78 TTM with a return rate of 11% driven by a
  supplier QC regression documented in quality_escalation.md (confidence 0.88).
- Cirrus revenue is $337,551.94 TTM with a 1.8% return rate.
- Solstice revenue is $383,761.96 TTM with a 12.7% return rate.

## Recommendations

| Recommendation | Owner | Expected Impact | Priority |
|---|---|---|---|
| Fix Tide supplier QC and pause discounts | Supply Chain | Recover ~$60k revenue | High |
| Rebalance Solstice pricing | Pricing team | +3pp margin | Medium |
| Re-run Cirrus marketing campaign | Marketing | +8% units | Medium |

## Risks & Limitations

- Historical data only; no forecast validation.
- Marketing attribution is correlational, not causal.

## Sources

- quality_escalation.md
- pricing_policy.md
- sales_sql trailing-12m query

## Next Actions

- Validate root causes with owners and track remediation impact next quarter.
"""


class ScriptedFakeLLM(BaseLLM):
    """Role-routed fake provider: deterministic JSON per agent/attempt."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, plan: dict | None = None) -> None:
        self.plan = plan or DEFAULT_PLAN
        self.calls: list[dict] = []

    # -- provider API -----------------------------------------------------
    def invoke(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        system = messages[0].content if messages else ""
        content = self._respond(system, messages)
        self.calls.append({"system": system[:120], "json_mode": json_mode})
        return LLMResponse(
            content=content,
            model=self.model,
            provider=self.provider,
            usage=TokenUsage(
                prompt_tokens=120,
                completion_tokens=90,
                total_tokens=210,
                model=self.model,
                provider=self.provider,
            ),
            latency_ms=1,
        )

    # -- routing ----------------------------------------------------------
    def _respond(self, system: str, messages: list[ChatMessage]) -> str:
        if "PLANNER agent" in system:
            return json.dumps(self.plan)
        if "RESEARCH agent" in system:
            return self._research(messages)
        if "ANALYST agent" in system:
            return self._analyst(system, messages)
        if "EXECUTION agent" in system:
            return self._execution(messages)
        if "REPORTING agent" in system:
            return self._reporting(messages)
        if "EVALUATOR agent" in system:
            return json.dumps(
                {
                    "score": 0.9,
                    "passed": True,
                    "criteria_met": ["task complete"],
                    "issues": [],
                    "feedback": "Good.",
                }
            )
        if "SYNTHESIZER agent" in system:
            return REPORT_MARKDOWN
        return json.dumps({"thought": "noop", "action": "final", "result": {"summary": "noop"}})

    @staticmethod
    def _attempt(system: str) -> int:
        marker = "ATTEMPT: "
        if marker in system:
            try:
                return int(system.split(marker, 1)[1].split(" ", 1)[0].rstrip("."))
            except (TypeError, ValueError):
                return 1
        return 1

    @staticmethod
    def _has_observation(messages: list[ChatMessage]) -> bool:
        return any("Observation from tool" in message.content for message in messages)

    def _research(self, messages: list[ChatMessage]) -> str:
        if not self._has_observation(messages):
            return json.dumps(
                {
                    "thought": "Search internal memos for root causes.",
                    "action": "tool",
                    "tool": "knowledge_search",
                    "args": {"query": "why are products underperforming quality pricing marketing"},
                }
            )
        return json.dumps(
            {
                "thought": "Cite the memos.",
                "action": "final",
                "result": {
                    "summary": (
                        "Internal memos attribute underperformance to a supplier quality "
                        "regression (Tide), discount dependency (Solstice) and weak marketing "
                        "(Cirrus)."
                    ),
                    "findings": [
                        {
                            "statement": "Tide returns trace to a supplier QC regression",
                            "evidence": "quality_escalation.md documents repeated returns",
                            "source": "quality_escalation.md",
                            "confidence": 0.88,
                        },
                        {
                            "statement": "Solstice depends on deep discounting",
                            "evidence": "pricing_policy.md notes 31% average discounts",
                            "source": "pricing_policy.md",
                            "confidence": 0.83,
                        },
                        {
                            "statement": "Cirrus returns are low, suggesting marketing weakness",
                            "evidence": "support taxonomy shows few quality tickets for Cirrus",
                            "source": "support_taxonomy.md",
                            "confidence": 0.76,
                        },
                    ],
                    "confidence": 0.82,
                },
            }
        )

    def _analyst(self, system: str, messages: list[ChatMessage]) -> str:
        attempt = self._attempt(system)
        if not self._has_observation(messages):
            return json.dumps(
                {
                    "thought": "Query trailing-12m revenue by product.",
                    "action": "tool",
                    "tool": "sales_sql",
                    "args": {
                        "query": (
                            "SELECT p.name, SUM(s.revenue) AS revenue FROM sales s "
                            "JOIN products p ON p.product_id = s.product_id "
                            "GROUP BY p.name ORDER BY revenue ASC LIMIT 3"
                        )
                    },
                }
            )
        if attempt <= 1:
            # Weak attempt: too short + no findings → evaluator fails → retry path.
            return json.dumps(
                {
                    "thought": "Fast answer.",
                    "action": "final",
                    "result": {"summary": "done", "findings": [], "confidence": 0.2},
                }
            )
        return json.dumps(
            {
                "thought": "Full quantified answer.",
                "action": "final",
                "result": {
                    "summary": (
                        "Trailing-12-month SQL analysis shows Tide Portable Speaker at "
                        "$138,612.78 revenue with an 11% return rate, Cirrus Air Purifier at "
                        "$337,551.94 with a 1.8% return rate and Solstice Ergonomic Chair at "
                        "$383,761.96 with a 12.7% return rate."
                    ),
                    "findings": [
                        {
                            "statement": "Tide is the worst performer at $138,612.78 TTM revenue",
                            "evidence": "sales_sql: revenue 138612.78, return_rate 0.11",
                            "source": "sales_sql trailing-12m",
                            "confidence": 0.9,
                        },
                        {
                            "statement": "Solstice has the highest return rate (12.7%)",
                            "evidence": "sales_sql: revenue 383761.96, return_rate 0.127",
                            "source": "sales_sql trailing-12m",
                            "confidence": 0.87,
                        },
                    ],
                    "confidence": 0.85,
                },
            }
        )

    def _execution(self, messages: list[ChatMessage]) -> str:
        if not self._has_observation(messages):
            plan_md = (
                "# Remediation Action Plan\n\n"
                "1. Fix Tide supplier QC — owner: Supply Chain — impact: recover ~$60k revenue.\n"
                "2. Rebalance Solstice pricing — owner: Pricing — impact: +3pp gross margin.\n"
                "3. Re-run Cirrus marketing — owner: Marketing — impact: +8% units.\n\n"
                "All pricing, discount and campaign actions are SENSITIVE and require human "
                "approval before execution.\n"
            )
            return json.dumps(
                {
                    "thought": "Persist the action plan.",
                    "action": "tool",
                    "tool": "report_writer",
                    "args": {
                        "title": "Remediation Action Plan",
                        "content": plan_md,
                        "description": "Prioritized remediation actions",
                    },
                }
            )
        return json.dumps(
            {
                "thought": "Report the plan.",
                "action": "final",
                "result": {
                    "summary": (
                        "Prioritized three remediation actions: fix Tide supplier QC, rebalance "
                        "Solstice pricing and re-run Cirrus marketing. All pricing/campaign "
                        "changes are marked sensitive and require approval."
                    ),
                    "findings": [
                        {
                            "statement": "Fix Tide supplier QC (owner: Supply Chain)",
                            "evidence": "QC regression drives 11% returns",
                            "source": "task-2 analysis + quality memo",
                            "confidence": 0.86,
                        },
                        {
                            "statement": "Rebalance Solstice pricing (owner: Pricing)",
                            "evidence": "22% discount dependency erodes margin",
                            "source": "task-2 analysis + pricing memo",
                            "confidence": 0.8,
                        },
                    ],
                    "confidence": 0.84,
                    "metrics": {"artifact_markdown": "# Remediation Action Plan\n\nActions..."},
                },
            }
        )

    def _reporting(self, messages: list[ChatMessage]) -> str:
        if not self._has_observation(messages):
            return json.dumps(
                {
                    "thought": "Persist the report.",
                    "action": "tool",
                    "tool": "report_writer",
                    "args": {
                        "title": "Business Performance Analysis",
                        "content": REPORT_MARKDOWN,
                        "description": "Final business report",
                    },
                }
            )
        return json.dumps(
            {
                "thought": "Report drafted.",
                "action": "final",
                "result": {
                    "summary": (
                        "Produced the executive business report covering the three worst "
                        "products, root causes, recommendations with owners and next actions."
                    ),
                    "findings": [
                        {
                            "statement": "Tide, Cirrus and Solstice are the worst performers",
                            "evidence": "trailing-12m revenue and return rates",
                            "source": "task-2 analysis",
                            "confidence": 0.88,
                        }
                    ],
                    "confidence": 0.87,
                    "artifact_markdown": REPORT_MARKDOWN,
                },
            }
        )


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def make_settings(tmp_path: Path, **overrides):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    base = get_settings()
    defaults = dict(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        artifacts_dir=str(artifacts),
        hitl_enabled=True,
        auto_approve_default=False,
        max_agent_retries=2,
        eval_pass_score=0.72,
        step_timeout_seconds=60,
        llm_max_calls_per_run=80,
        llm_provider="fake",
        llm_model="fake-1",
        knowledge_dir=str(tmp_path / "knowledge"),
        sales_db_path=str(tmp_path / "sales.db"),
    )
    defaults.update(overrides)
    return dataclasses.replace(base, **defaults)


def prepare(tmp_path: Path, **settings_overrides):
    settings = make_settings(tmp_path, **settings_overrides)
    configure_database(settings.database_url)
    init_db()
    registry = build_stub_registry()
    fake = ScriptedFakeLLM()
    service = WorkflowService(llm=fake, settings=settings, registry=registry)
    return service, fake, settings


def run_graph(service: WorkflowService, objective: str, *, auto_approve: bool = False) -> str:
    return asyncio.run(service.run_sync(objective, user_id=None, auto_approve=auto_approve))


def wait_for(predicate, *, timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def get_run(run_id: str) -> Run:
    with session_scope() as session:
        run = session.get(Run, run_id)
        assert run is not None
        return run


def get_task_rows(run_id: str) -> list[TaskRecord]:
    with session_scope() as session:
        return list(
            session.execute(select(TaskRecord).where(TaskRecord.run_id == run_id)).scalars()
        )


def get_events(run_id: str) -> list[RunEvent]:
    with session_scope() as session:
        return list(
            session.execute(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
            ).scalars()
        )


SALES_OBJECTIVE = (
    "Analyze our sales performance, identify the three worst-performing products, "
    "investigate why they're underperforming, and generate a report with recommendations."
)

SENSITIVE_OBJECTIVE = (
    "Analyze our sales performance, identify the three worst-performing products, and "
    "launch a discount campaign plus send an announcement email to customers."
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_plan_task_loop_evaluation_retry_and_final_report(tmp_path):
    service, fake, _ = prepare(tmp_path)

    run_id = run_graph(service, SALES_OBJECTIVE)
    run = get_run(run_id)

    assert run.status == "completed", f"run failed: {run.error}"
    assert run.final_report and len(run.final_report) > 500
    assert "## Executive Summary" in run.final_report
    assert run.plan_json and len(run.plan_json["tasks"]) == 4
    assert run.started_at is not None and run.completed_at is not None

    # -- task loop + retry path -----------------------------------------
    tasks = {task.task_id: task for task in get_task_rows(run_id)}
    assert set(tasks) == {"task-1", "task-2", "task-3", "task-4"}
    assert tasks["task-1"].status == "completed"
    assert tasks["task-2"].status == "completed"
    # Retry path: attempt 1 failed deterministic rules, attempt 2 passed.
    assert tasks["task-2"].attempts == 2, "analyst should have been retried once"
    assert tasks["task-2"].evaluation_json["passed"] is True
    assert tasks["task-2"].evaluation_json["score"] > 0.0
    assert tasks["task-3"].status == "completed"
    assert tasks["task-4"].status == "completed"

    # -- token usage aggregated ------------------------------------------
    usage = run.token_usage_json
    assert usage["calls"] > 0
    assert usage["total_tokens"] > 0
    assert usage["provider"] == "fake"

    # -- durable event log: monotonic, gap-free seq + expected types -----
    events = get_events(run_id)
    seqs = [event.seq for event in events]
    assert seqs == list(range(1, len(events) + 1))
    types = {event.type for event in events}
    for expected in (
        "run.started",
        "plan.created",
        "task.started",
        "tool.called",
        "agent.completed",
        "evaluation.completed",
        "task.retry",
        "report.created",
        "run.completed",
    ):
        assert expected in types, f"missing event type {expected}"
    completed_event = next(event for event in events if event.type == "run.completed")
    assert completed_event.data_json.get("report_chars") == len(run.final_report)

    # -- artifact persisted + report file on disk -----------------------
    with session_scope() as session:
        artifacts = list(
            session.execute(select(Artifact).where(Artifact.run_id == run_id)).scalars()
        )
    assert artifacts, "expected at least one artifact row"
    report_artifact_id = completed_event.data_json.get("artifact_id")
    report_artifact = next(
        (artifact for artifact in artifacts if artifact.id == report_artifact_id),
        max(artifacts, key=lambda a: len(a.content or "")),
    )
    assert report_artifact.content == run.final_report
    assert Path(report_artifact.path).exists()
    assert Path(report_artifact.path).read_text(encoding="utf-8").startswith("#")


def test_hitl_approval_pause_resume_and_reject_paths(tmp_path):
    service, fake, _ = prepare(tmp_path)

    # ---- approved path -------------------------------------------------
    run_id = run_graph(service, SENSITIVE_OBJECTIVE, auto_approve=False)
    run = get_run(run_id)
    assert run.status == "awaiting_approval"

    with session_scope() as session:
        approval = session.execute(
            select(Approval).where(Approval.run_id == run_id)
        ).scalar_one()
        assert approval.status == "pending"
        approval_id = approval.id
        assert approval.task_id == "task-3"  # sensitive execution task
        approval.status = "approved"
        approval.decided_by = "tester"
        approval.note = "approved in unit test"
        approval.decided_at = datetime.now(timezone.utc)

    events = get_events(run_id)
    assert "approval.required" in {event.type for event in events}
    assert "run.awaiting_approval" in {event.type for event in events}

    asyncio.run(service.resume_run(run_id))
    assert wait_for(lambda: get_run(run_id).status in {"completed", "failed", "cancelled"}), (
        f"run did not resume: {get_run(run_id).status}"
    )
    run = get_run(run_id)
    assert run.status == "completed", f"run failed: {run.error}"
    tasks = {task.task_id: task for task in get_task_rows(run_id)}
    assert tasks["task-3"].status == "completed"
    assert run.final_report and len(run.final_report) > 500
    types = {event.type for event in get_events(run_id)}
    assert "approval.resolved" in types

    # ---- rejected path -------------------------------------------------
    service2, _, _ = prepare(tmp_path / "reject")
    run_id = run_graph(service2, SENSITIVE_OBJECTIVE, auto_approve=False)
    assert get_run(run_id).status == "awaiting_approval"
    with session_scope() as session:
        approval = session.execute(
            select(Approval).where(Approval.run_id == run_id)
        ).scalar_one()
        approval.status = "rejected"
        approval.decided_by = "tester"
        approval.note = "not this quarter"
        approval.decided_at = datetime.now(timezone.utc)
    asyncio.run(service2.resume_run(run_id))
    assert wait_for(lambda: get_run(run_id).status in {"completed", "failed", "cancelled"})
    run = get_run(run_id)
    assert run.status == "completed", f"run failed: {run.error}"
    tasks = {task.task_id: task for task in get_task_rows(run_id)}
    assert tasks["task-3"].status == "skipped"
    assert run.final_report and len(run.final_report) > 500
    types = {event.type for event in get_events(run_id)}
    assert "approval.resolved" in types


def test_budget_exhaustion_degrades_gracefully(tmp_path):
    service, fake, _ = prepare(tmp_path, llm_max_calls_per_run=1)

    run_id = run_graph(service, SALES_OBJECTIVE)
    run = get_run(run_id)

    # With the budget exhausted the agents cannot produce results: the run must fail
    # honestly (no successful tasks) rather than claim success with an empty report.
    assert run.status == "failed"
    assert run.error and "No tasks completed successfully" in run.error
    assert run.token_usage_json["calls"] <= 1
    # No crash: task records persisted and evaluated; diagnostic artifact still written.
    tasks = get_task_rows(run_id)
    assert tasks, "task records should exist even when the LLM budget is exhausted"
    assert all(task.status in {"completed", "failed", "skipped"} for task in tasks)
    with session_scope() as session:
        artifacts = list(
            session.execute(select(Artifact).where(Artifact.run_id == run_id)).scalars()
        )
    assert artifacts, "the deterministic fallback report should still be written as an artifact"
