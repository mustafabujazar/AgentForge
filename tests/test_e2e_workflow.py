"""End-to-end workflow tests: real LangGraph orchestration, fake LLM, no network.

What is *real* here
-------------------
* ``WorkflowService`` (background scheduling, runtimes, checkpointer, HITL resume)
* the LangGraph state machine (intake → plan → dispatch → route → execute →
  evaluate → retry/advance → synthesize → finalize)
* the four role agents and their bounded ReAct tool loops
* the real tool registry: ``knowledge_search`` (in-process RAG), ``sales_sql``
  (read-only SQLite over ``data/demo/sales.db``), ``report_writer`` (file
  artifacts), and the synthesizer artifact persistence
* all database writes (runs, task_records, evaluation_metrics, artifacts,
  run_events) and the SSE durable event log

What is fake
------------
Only the LLM provider: a :class:`WorkflowResponder` answers the planner,
agent, evaluator and synthesizer prompts deterministically.  Network access is
blocked session-wide by ``tests/conftest.py``, so a real provider can never be
reached even if a scripted prompt is missed.
"""

from __future__ import annotations

import asyncio
import json
import re
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
)
from app.orchestrator import persistence
from app.orchestrator.service import WorkflowService

pytestmark = [pytest.mark.e2e, pytest.mark.offline]

ANALYSIS_OBJECTIVE = (
    "Analyze our sales performance, identify the three worst-performing products, "
    "investigate why they are underperforming, and generate a report with recommendations."
)
ACTION_OBJECTIVE = (
    "Launch a remediation campaign: apply a 5% discount to the three worst-performing "
    "products, notify the sales team, and report the results."
)

RESEARCH_QUERY = "quality escalation pricing policy marketing playbook underperformance"
ANALYST_SQL = (
    "SELECT p.name, ROUND(SUM(s.revenue), 2) AS revenue, "
    "ROUND(SUM(s.revenue - s.cost), 2) AS gross_profit, SUM(s.quantity) AS units, "
    "ROUND(SUM(CASE WHEN s.returned=1 THEN 1 ELSE 0 END)*100.0/COUNT(s.sale_id),2) "
    "AS return_rate_pct, ROUND(AVG(s.discount)*100,2) AS avg_discount_pct "
    "FROM sales s JOIN products p ON p.product_id = s.product_id "
    "WHERE s.sale_date >= date((SELECT MAX(sale_date) FROM sales), 'start of month', '-11 months') "
    "GROUP BY p.product_id ORDER BY revenue ASC LIMIT 3"
)

ACTION_PLAN_MD = """# Remediation Action Plan

## Context

Three products underperform for distinct reasons: quality-escalation delays,
discount dependency and weak marketing support.

## Actions

| # | Action | Owner | Expected Impact | Effort | Risk |
|---|---|---|---|---|---|
| 1 | Fix the 14-day quality escalation lag for the portable audio line | Quality / Support | Recover ~8% of returned units | Medium | Low |
| 2 | Cap discretionary discounts at 20% for the ergonomic chair | Sales Ops | Improve gross margin by ~3pp | Low | Medium |
| 3 | Reallocate 15% of marketing spend to the air purifier launch | Marketing | Lift units ~12% | Medium | Medium |
| 4 | Weekly underperformance review with the owning teams | Operations | Faster detection | Low | Low |

## Sensitive Steps (require human approval)

- Pricing and discount changes.
- Campaign launch and customer notifications.
- Refunds and supplier escalation.

## Limitations

The plan is based on historical demo data and must be validated by the owning teams.
"""

REPORT_MD = """# Business Analysis Report: Sales Underperformance

## Executive Summary

Three products account for a disproportionate share of revenue decline. The
analysis combines SQL evidence over the demo sales database with internal
process memos to explain why each product underperforms and what to do next.

## Key Findings & Evidence

- The portable audio line has the lowest trailing revenue at $138,613, with a
  3.26% return rate in the SQL result.
- The air purifier generated $337,552 in trailing revenue with a 29.06% average
  discount.
- The ergonomic chair generated $383,762 in trailing revenue, with a 12.36%
  return rate in the SQL result.

## Recommendations

| Recommendation | Owner | Expected Impact | Priority |
|---|---|---|---|
| Fix quality escalation lag | Quality / Support | Recover returned units | High |
| Cap discretionary discounts | Sales Ops | +3pp gross margin | High |
| Rebalance marketing to launch product | Marketing | +12% units | Medium |

## Risks & Limitations

- Figures come from the demo dataset and are historical, not forecasts.
- Root causes are evidence-backed hypotheses, not proven causal effects.

## Sources

- quality_escalation.md
- pricing_policy.md
- marketing_playbook.md
- sales_sql: trailing twelve month revenue by product

## Next Actions

- Validate findings with product owners.
- Approve the pricing and campaign actions through the HITL gate.
- Re-run the analysis after remediation to measure impact.
"""

SYNTHESIZED_REPORT_MD = REPORT_MD + """
## Appendix: Run Metadata

This report was synthesized by AgentForge from four task results, each evaluated
before being handed to the next agent. Token usage and the full event trace are
available on the run detail page.
"""

JUDGE_PASS = {
    "score": 0.95,
    "passed": True,
    "criteria_met": ["answers task", "evidence backed", "success criteria met"],
    "issues": [],
    "feedback": "Strong, evidence-backed result that the next agent can use.",
}


def _finding(statement: str, evidence: str, source: str, confidence: float = 0.8) -> dict[str, Any]:
    return {
        "statement": statement,
        "evidence": evidence,
        "source": source,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Scripted responder: one deterministic answer per workflow role
# ---------------------------------------------------------------------------
class WorkflowResponder:
    """Maps a prompt (detected from its system message) to a scripted response."""

    def __init__(self, *, force_retry_role: str | None = None) -> None:
        self.force_retry_role = force_retry_role
        self.prompts: list[str] = []
        self.roles: list[str] = []

    # -- BaseLLM.Responder signature --------------------------------------
    def __call__(self, messages, json_mode: bool) -> str:
        system = next((m.content for m in messages if getattr(m, "role", "") == "system"), "")
        user = next(
            (m.content for m in reversed(messages) if getattr(m, "role", "") == "user"), ""
        )
        self.prompts.append(user)
        role = self._detect_role(system)
        self.roles.append(role)
        if role == "planner":
            return json.dumps(self._plan())
        if role == "evaluator":
            return json.dumps(JUDGE_PASS)
        if role == "synthesizer":
            return SYNTHESIZED_REPORT_MD
        if role in {"research", "analyst", "execution", "reporting"}:
            return json.dumps(self._agent_step(role, system, messages))
        return json.dumps({"thought": "unrecognised prompt", "action": "final", "result": {}})

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _detect_role(system: str) -> str:
        for role in ("PLANNER", "EVALUATOR", "SYNTHESIZER"):
            if f"You are the {role} agent" in system:
                return role.lower()
        for role in ("RESEARCH", "ANALYST", "EXECUTION", "REPORTING"):
            if f"You are the {role} agent" in system:
                return role.lower()
        return "unknown"

    @staticmethod
    def _attempt(system: str) -> int:
        match = re.search(r"ATTEMPT:\s*(\d+)", system)
        return int(match.group(1)) if match else 1

    def _plan(self) -> dict[str, Any]:
        return {
            "objective": ANALYSIS_OBJECTIVE,
            "summary": (
                "Research root causes, quantify performance with SQL, draft a remediation "
                "action plan, then synthesize an executive business report."
            ),
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Investigate root causes and internal context",
                    "description": "Search internal memos for quality, pricing and marketing context.",
                    "agent": "research",
                    "depends_on": [],
                    "tool_hints": ["knowledge_search"],
                    "expected_output": "3+ evidence-backed findings with sources.",
                    "success_criteria": ["At least 3 findings cite a source"],
                    "priority": 2,
                },
                {
                    "id": "task-2",
                    "title": "Quantify performance with SQL analysis",
                    "description": "Query the sales database and rank products by revenue.",
                    "agent": "analyst",
                    "depends_on": ["task-1"],
                    "tool_hints": ["sales_sql"],
                    "expected_output": "Quantified product ranking.",
                    "success_criteria": ["Trailing revenue computed with SQL"],
                    "priority": 1,
                },
                {
                    "id": "task-3",
                    "title": "Draft the remediation action plan",
                    "description": "Turn the analysis into a prioritized action plan.",
                    "agent": "execution",
                    "depends_on": ["task-2"],
                    "tool_hints": ["report_writer"],
                    "expected_output": "Prioritized action plan with owners.",
                    "success_criteria": ["Actions are owned and prioritized"],
                    "priority": 3,
                },
                {
                    "id": "task-4",
                    "title": "Compose the final business report",
                    "description": "Synthesize everything into an executive Markdown report.",
                    "agent": "reporting",
                    "depends_on": ["task-2", "task-3"],
                    "tool_hints": ["report_writer"],
                    "expected_output": "Full Markdown business report.",
                    "success_criteria": ["Report is complete and cited"],
                    "priority": 2,
                },
            ],
            "success_criteria": [
                "Identify the worst-performing products with quantified evidence",
                "Explain root causes with sources",
                "Deliver a complete Markdown report",
            ],
            "requires_approval": False,
        }

    def _tool_action(self, role: str) -> dict[str, Any]:
        if role == "research":
            return {
                "thought": "Search internal memos for the three underperformance stories.",
                "action": "tool",
                "tool": "knowledge_search",
                "args": {"query": RESEARCH_QUERY, "k": 3},
            }
        if role == "analyst":
            return {
                "thought": "Quantify revenue by product with SQL.",
                "action": "tool",
                "tool": "sales_sql",
                "args": {"query": ANALYST_SQL},
            }
        if role == "execution":
            return {
                "thought": "Persist the remediation action plan as an artifact.",
                "action": "tool",
                "tool": "report_writer",
                "args": {
                    "title": "Remediation Action Plan",
                    "content": ACTION_PLAN_MD,
                    "name": "remediation-action-plan.md",
                    "description": "Prioritized remediation plan with owners and risks.",
                },
            }
        return {
            "thought": "Persist the draft report as an artifact.",
            "action": "tool",
            "tool": "report_writer",
            "args": {
                "title": "Business Analysis Report",
                "content": REPORT_MD,
                "name": "business-analysis-report.md",
                "description": "Full business analysis report.",
            },
        }

    def _agent_step(self, role: str, system: str, messages) -> dict[str, Any]:
        has_observation = any(
            getattr(m, "role", "") == "user" and "Observation from tool" in m.content
            for m in messages
        )
        if not has_observation:
            return self._tool_action(role)
        attempt = self._attempt(system)
        return {"thought": "Evidence collected.", "action": "final", "result": self._final_payload(role, attempt)}

    def _final_payload(self, role: str, attempt: int) -> dict[str, Any]:
        if role == "research":
            return {
                "summary": (
                    "Internal memos explain the three underperformance stories: quality "
                    "escalation delays, discount dependency and weak marketing support."
                ),
                "findings": [
                    _finding(
                        "Quality escalation lags 14 days for defect-heavy products",
                        "The quality escalation memo records a 14-day median defect escalation.",
                        "quality_escalation.md",
                        0.82,
                    ),
                    _finding(
                        "Discretionary discounts exceed the pricing policy guidance",
                        "Pricing policy caps discretionary discounts at 20% but the chair averages 31%.",
                        "pricing_policy.md",
                        0.78,
                    ),
                    _finding(
                        "Launch products receive disproportionately little marketing spend",
                        "Marketing playbook requires 15% launch support; the air purifier received 6%.",
                        "marketing_playbook.md",
                        0.75,
                    ),
                ],
                "confidence": 0.82,
            }
        if role == "analyst":
            if self.force_retry_role == "analyst" and attempt <= 1:
                # Deliberately weak first attempt: no findings, no evidence → evaluator retries.
                return {"summary": "done", "findings": [], "confidence": 0.1}
            return {
                "summary": (
                    "Trailing-twelve-month SQL analysis ranks the portable audio line last at "
                    "$138,613 revenue, followed by the air purifier at $337,552 and the "
                    "ergonomic chair at $383,762. The query also compared return and "
                    "discount rates."
                ),
                "findings": [
                    _finding(
                        "Portable audio has the lowest trailing revenue",
                        "$138,613 trailing revenue with a 3.26% return rate.",
                        "sales_sql: trailing-12m revenue and return rate by product",
                        0.9,
                    ),
                    _finding(
                        "Air purifier has the highest average discount",
                        "$337,552 trailing revenue and a 29.06% average discount.",
                        "sales_sql: trailing-12m revenue and discount rate by product",
                        0.86,
                    ),
                    _finding(
                        "Ergonomic chair has the highest return rate among these products",
                        "$383,762 trailing revenue and a 12.36% return rate.",
                        "sales_sql: trailing-12m revenue and return rate by product",
                        0.8,
                    ),
                ],
                "confidence": 0.88,
            }
        if role == "execution":
            return {
                "summary": (
                    "Prioritized remediation plan with four owned actions, expected impact and "
                    "explicit sensitive-step flags for pricing, campaigns and communications."
                ),
                "findings": [
                    _finding(
                        "Quality escalation fix is the highest-impact action",
                        "Reducing escalation lag should recover roughly 8% of returned units.",
                        "analysis task-2",
                        0.8,
                    )
                ],
                "artifact_markdown": ACTION_PLAN_MD,
                "confidence": 0.85,
            }
        return {
            "summary": (
                "Executive report synthesizing SQL evidence and internal memos into ranked "
                "findings, owned recommendations, risks, sources and next actions."
            ),
            "findings": [
                _finding(
                    "Three products drive the revenue decline",
                    "Portable audio, ergonomic chair and air purifier explain most of the drop.",
                    "analysis task-2",
                    0.85,
                ),
                _finding(
                    "Remediation requires governed pricing and campaign changes",
                    "Pricing, discount and campaign actions are flagged sensitive for approval.",
                    "action plan task-3",
                    0.8,
                ),
            ],
            "artifact_markdown": REPORT_MD,
            "confidence": 0.88,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def seed_knowledge() -> None:
    """Ingest internal memos so ``knowledge_search`` returns real hits."""
    from app.rag.store import get_knowledge_store

    store = get_knowledge_store()
    store.ingest_text(
        "Quality Escalation Process",
        "quality_escalation.md",
        "# Quality Escalation Process\n\nDefect escalations currently take a 14-day median "
        "before the quality team triages them. Products with repeated mechanism failures are "
        "flagged for supplier review and replacement under the 36-month warranty.",
        metadata={"team": "quality"},
    )
    store.ingest_text(
        "Pricing and Discount Policy",
        "pricing_policy.md",
        "# Pricing and Discount Policy\n\nDiscretionary discounts above 20% require finance "
        "approval. Chairs and audio products have drifted to 31% average discounts, which "
        "compresses gross margin and trains customers to wait for promotions.",
        metadata={"team": "finance"},
    )
    store.ingest_text(
        "Marketing Playbook",
        "marketing_playbook.md",
        "# Marketing Playbook\n\nLaunch products require 15% marketing support. "
        "The air purifier currently receives 6% of the portfolio marketing budget.",
        metadata={"team": "marketing"},
    )


def fetch_run(db_session, run_id: str) -> Run:
    db_session.expire_all()
    run = db_session.get(Run, run_id)
    assert run is not None, f"run {run_id} not found"
    return run


def task_rows(db_session, run_id: str) -> list[TaskRecord]:
    db_session.expire_all()
    return list(
        db_session.execute(
            select(TaskRecord).where(TaskRecord.run_id == run_id).order_by(TaskRecord.task_id)
        ).scalars()
    )


def event_rows(db_session, run_id: str) -> list[RunEvent]:
    db_session.expire_all()
    return list(
        db_session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        ).scalars()
    )


def artifact_rows(db_session, run_id: str) -> list[Artifact]:
    db_session.expire_all()
    return list(
        db_session.execute(
            select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
        ).scalars()
    )


def evaluation_rows(db_session, run_id: str) -> list[EvaluationMetric]:
    db_session.expire_all()
    return list(
        db_session.execute(
            select(EvaluationMetric).where(EvaluationMetric.run_id == run_id)
        ).scalars()
    )


def join_service_threads(service: WorkflowService, run_id: str, timeout: float = 15.0) -> None:
    """Wait for any background worker for ``run_id`` so teardown never races a write."""
    thread = service._threads.get(run_id)
    if thread is not None:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), f"workflow thread for {run_id} did not finish"


def api_run(client, headers: dict[str, str], run_id: str) -> dict[str, Any]:
    response = client.get(f"/api/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# Tests
# ===========================================================================
class TestFullWorkflow:
    def test_full_workflow_offline(self, scripted_llm, db_session, settings):
        seed_knowledge()
        responder = WorkflowResponder()
        llm = scripted_llm(responder=responder)
        service = WorkflowService(llm=llm)

        run_id = asyncio.run(service.run_sync(ANALYSIS_OBJECTIVE))

        run = fetch_run(db_session, run_id)
        assert run.status == "completed", f"run status={run.status} error={run.error}"
        assert run.error is None
        assert run.completed_at is not None
        assert run.final_report and len(run.final_report) > 600
        assert "## Executive Summary" in run.final_report
        assert run.started_at is not None

        # --- plan ---------------------------------------------------------
        assert run.plan_json is not None
        plan_tasks = run.plan_json["tasks"]
        assert len(plan_tasks) == 4
        assert [task["id"] for task in plan_tasks] == ["task-1", "task-2", "task-3", "task-4"]
        assert [task["agent"] for task in plan_tasks] == [
            "research",
            "analyst",
            "execution",
            "reporting",
        ]
        assert not any(task["sensitive"] for task in plan_tasks)  # analysis objective

        # --- task records + evaluations ----------------------------------
        tasks = {record.task_id: record for record in task_rows(db_session, run_id)}
        assert set(tasks) == {"task-1", "task-2", "task-3", "task-4"}
        for task_id, record in tasks.items():
            assert record.status == "completed", (
                f"{task_id} status={record.status} evaluation={record.evaluation_json}"
            )
            assert record.attempts == 1
            assert record.evaluation_json is not None, f"{task_id} has no evaluation"
            assert record.evaluation_json["passed"] is True
            assert record.evaluation_json["score"] >= 0.72
            assert record.result_json is not None
            assert record.result_json["status"] == "completed"

        metrics = evaluation_rows(db_session, run_id)
        assert len(metrics) == 4
        assert all(metric.passed for metric in metrics)
        assert min(metric.score for metric in metrics) >= 0.72

        # --- real tool use -------------------------------------------------
        analyst_result = tasks["task-2"].result_json
        analyst_calls = analyst_result["tool_calls"]
        assert analyst_calls, "analyst used no tools"
        assert analyst_calls[0]["tool"] == "sales_sql"
        assert analyst_calls[0]["ok"] is True
        assert "rows" in analyst_calls[0]["output_preview"]

        research_calls = tasks["task-1"].result_json["tool_calls"]
        assert research_calls and research_calls[0]["tool"] == "knowledge_search"
        assert research_calls[0]["ok"] is True

        artifact_tools = [
            call["tool"]
            for task_id in ("task-3", "task-4")
            for call in tasks[task_id].result_json["tool_calls"]
        ]
        assert "report_writer" in artifact_tools

        # --- artifacts on disk + DB ---------------------------------------
        artifacts = artifact_rows(db_session, run_id)
        # The synthesizer persists the final report, and the executor persists the
        # agent-produced report_writer artifacts as first-class rows.
        assert len(artifacts) >= 3, (
            "expected the final report + agent artifacts as Artifact rows, got "
            f"{[artifact.name for artifact in artifacts]}"
        )
        for artifact in artifacts:
            assert artifact.path
            from pathlib import Path

            path = Path(artifact.path)
            assert path.is_file(), f"artifact file missing: {artifact.path}"
            assert path.stat().st_size > 0
        assert any("#" in (artifact.content or "") for artifact in artifacts)
        assert run.final_report in [
            (artifact.content or "") for artifact in artifacts
        ], "runs.final_report should match a persisted artifact"

        # Agent-level tool artifacts are written to disk, referenced in the task
        # results AND persisted as Artifact rows (executor._persist_artifacts).
        agent_artifact_paths = [
            artifact["path"]
            for task_id in ("task-3", "task-4")
            for artifact in (tasks[task_id].result_json.get("artifacts") or [])
            if artifact.get("path")
        ]
        assert agent_artifact_paths, "agents did not report any artifact from report_writer"
        from pathlib import Path

        persisted_paths = {artifact.path for artifact in artifacts}
        for artifact_path in agent_artifact_paths:
            assert Path(artifact_path).is_file(), f"agent artifact missing on disk: {artifact_path}"
            assert artifact_path in persisted_paths, (
                f"agent artifact {artifact_path} was not persisted as an Artifact row; "
                f"persisted={sorted(persisted_paths)}"
            )

        # --- events --------------------------------------------------------
        events = event_rows(db_session, run_id)
        assert events, "no run events persisted"
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        types = [event.type for event in events]
        for required in (
            "run.created",
            "run.started",
            "plan.created",
            "task.started",
            "tool.called",
            "agent.completed",
            "evaluation.completed",
            "report.created",
            "run.completed",
        ):
            assert required in types, f"missing event type {required}; got {sorted(set(types))}"
        assert types.count("task.started") == 4
        assert types.count("evaluation.completed") == 4
        called_tools = {
            event.data_json.get("tool") for event in events if event.type == "tool.called"
        }
        assert {"sales_sql", "knowledge_search", "report_writer"} <= called_tools
        completed_event = events[-1]
        assert completed_event.type == "run.completed"
        assert completed_event.data_json["token_usage"]["total_tokens"] > 0

        # --- token usage + state snapshot ---------------------------------
        usage = run.token_usage_json or {}
        assert usage["calls"] > 0
        assert usage["total_tokens"] > 0
        assert usage["provider"] == "mock"
        state = run.state_json or {}
        assert set(state.get("results") or {}) == {"task-1", "task-2", "task-3", "task-4"}
        assert state.get("final_report")

        # The scripted provider was actually exercised end to end.  The evaluator
        # short-circuits the LLM judge when deterministic rules already pass clearly,
        # so the judge may legitimately be absent from a clean happy-path run.
        assert len(responder.roles) >= 10
        assert {
            "planner",
            "research",
            "analyst",
            "execution",
            "reporting",
            "synthesizer",
        } <= set(responder.roles)
        assert responder.roles.index("planner") == 0
        assert responder.roles[-1] == "synthesizer"

    def test_retry_path_below_threshold_then_pass(self, scripted_llm, db_session):
        seed_knowledge()
        responder = WorkflowResponder(force_retry_role="analyst")
        llm = scripted_llm(responder=responder)
        service = WorkflowService(llm=llm)

        run_id = asyncio.run(service.run_sync(ANALYSIS_OBJECTIVE))

        run = fetch_run(db_session, run_id)
        assert run.status == "completed", f"run status={run.status} error={run.error}"
        assert run.final_report

        tasks = {record.task_id: record for record in task_rows(db_session, run_id)}
        assert tasks["task-2"].attempts == 2, "analyst task should have been retried once"
        assert tasks["task-2"].status == "completed"
        assert tasks["task-2"].evaluation_json["passed"] is True

        events = event_rows(db_session, run_id)
        retry_events = [event for event in events if event.type == "task.retry"]
        assert len(retry_events) == 1
        assert retry_events[0].task_id == "task-2"
        assert retry_events[0].data_json["attempt"] == 2
        # The retry decision is driven by hard rule failures, not only the blended score.
        assert retry_events[0].data_json["feedback"]

        analyst_evals = [
            event
            for event in events
            if event.type == "evaluation.completed" and event.task_id == "task-2"
        ]
        assert len(analyst_evals) == 2
        assert analyst_evals[0].data_json["passed"] is False
        assert analyst_evals[0].data_json["attempt"] == 1
        assert analyst_evals[1].data_json["passed"] is True
        assert analyst_evals[1].data_json["attempt"] == 2
        # A clearly failing attempt always reaches the LLM judge (no short-circuit).
        assert "evaluator" in responder.roles

        # Other tasks were not retried.
        assert all(
            record.attempts == 1
            for task_id, record in tasks.items()
            if task_id != "task-2"
        )

    def test_auto_approve_runs_sensitive_objective_without_pause(self, scripted_llm, db_session):
        seed_knowledge()
        responder = WorkflowResponder()
        service = WorkflowService(llm=scripted_llm(responder=responder))

        run_id = asyncio.run(service.run_sync(ACTION_OBJECTIVE, auto_approve=True))

        run = fetch_run(db_session, run_id)
        assert run.status == "completed"
        assert run.plan_json is not None
        execution_tasks = [
            task
            for task in run.plan_json["tasks"]
            if task["agent"] == "execution" and task["sensitive"]
        ]
        assert execution_tasks, "action objective should mark the execution task sensitive"
        approval_rows = list(
            db_session.execute(select(Approval).where(Approval.run_id == run_id)).scalars()
        )
        assert approval_rows == []  # auto-approved: no human gate needed

    def test_agent_artifacts_are_persisted_as_rows(self, scripted_llm, db_session):
        """Agent tool artifacts become Artifact rows (not just files + task JSON)."""
        seed_knowledge()
        service = WorkflowService(llm=scripted_llm(responder=WorkflowResponder()))
        run_id = asyncio.run(service.run_sync(ANALYSIS_OBJECTIVE))

        artifacts = artifact_rows(db_session, run_id)
        persisted_paths = {artifact.path for artifact in artifacts}
        agent_paths = {
            artifact["path"]
            for task in task_rows(db_session, run_id)
            for artifact in (task.result_json or {}).get("artifacts") or []
            if artifact.get("path")
        }
        assert agent_paths, "execution/reporting agents produced no artifact refs"
        assert agent_paths <= persisted_paths, (
            "agent artifact refs missing from the artifacts table: "
            f"{sorted(agent_paths - persisted_paths)}"
        )
        # The synthesized final report is also persisted exactly once.
        reports = [
            artifact
            for artifact in artifacts
            if artifact.name.endswith(".md") and artifact.content and "# " in artifact.content
        ]
        assert reports
        # No duplicate rows for the same path (executor.ensure_artifact dedupes).
        assert len(persisted_paths) == len(artifacts)


class TestApprovalGate:
    def _start_service(self, monkeypatch, scripted_llm) -> WorkflowService:
        import app.api.runs as runs_api

        responder = WorkflowResponder()
        service = WorkflowService(llm=scripted_llm(responder=responder))
        monkeypatch.setattr(runs_api, "workflow_service", service)
        return service

    def test_approval_pause_then_approve_completes(
        self, client, auth_headers, scripted_llm, monkeypatch, db_session, wait_for
    ):
        service = self._start_service(monkeypatch, scripted_llm)
        create = client.post(
            "/api/runs",
            json={"objective": ACTION_OBJECTIVE, "auto_approve": False},
            headers=auth_headers,
        )
        assert create.status_code == 202, create.text
        run_id = create.json()["id"]
        try:
            paused = wait_for(
                lambda: (
                    api_run(client, auth_headers, run_id)
                    if api_run(client, auth_headers, run_id)["status"] == "awaiting_approval"
                    else None
                ),
                timeout=25,
                message="run to pause for approval",
            )
            approval = paused["pending_approval"]
            assert approval, f"no pending approval exposed: {paused}"
            assert paused["plan"] is not None
            assert client.get("/api/runs/%s/events" % run_id, headers=auth_headers).status_code == 200

            # Durable approval row + paused task record.
            db_session.expire_all()
            row = db_session.get(Approval, approval["id"])
            assert row is not None and row.status == "pending"
            assert row.requested_action
            assert row.run_id == run_id
            db_status = persistence.run_status(run_id)[0]
            assert db_status == "awaiting_approval"
            events_before = client.get(
                f"/api/runs/{run_id}/events", headers=auth_headers
            ).json()
            types_before = [event["type"] for event in events_before]
            assert "approval.required" in types_before
            assert "run.awaiting_approval" in types_before

            decide = client.post(
                f"/api/runs/{run_id}/approvals/{approval['id']}",
                json={"decision": "approved", "note": "QA approved the campaign"},
                headers=auth_headers,
            )
            assert decide.status_code == 200, decide.text

            final = wait_for(
                lambda: (
                    api_run(client, auth_headers, run_id)
                    if api_run(client, auth_headers, run_id)["status"]
                    in {"completed", "failed", "cancelled"}
                    else None
                ),
                timeout=40,
                message="run to reach a terminal status after approval",
            )
            assert final["status"] == "completed", final
            assert final["final_report"] and "## Executive Summary" in final["final_report"]
            assert final["pending_approval"] is None

            # Artifacts are persisted and downloadable through the API.
            report = client.get(f"/api/runs/{run_id}/report", headers=auth_headers).json()
            assert report["artifacts"], "no artifacts persisted for the approved run"
            artifact_id = report["artifacts"][0]["id"]
            download = client.get(
                f"/api/artifacts/{artifact_id}/download", headers=auth_headers
            )
            assert download.status_code == 200
            assert download.text

            # DB evidence for the approved decision + resumed event.
            db_session.expire_all()
            decided = db_session.get(Approval, approval["id"])
            assert decided.status == "approved"
            assert decided.note == "QA approved the campaign"
            assert decided.decided_by is not None
            events_after = client.get(f"/api/runs/{run_id}/events", headers=auth_headers).json()
            types_after = [event["type"] for event in events_after]
            assert "approval.resolved" in types_after
            assert "run.completed" in types_after
            resolved = [e for e in events_after if e["type"] == "approval.resolved"]
            assert resolved[0]["data"]["decision"] == "approved"

            tasks = task_rows(db_session, run_id)
            execution_task = next(task for task in tasks if task.agent == "execution")
            assert execution_task.status == "completed"
            assert execution_task.result_json["metrics"]["approval_granted"] is True
        finally:
            join_service_threads(service, run_id)

    def test_approval_rejection_skips_task_and_still_reports(
        self, client, auth_headers, scripted_llm, monkeypatch, db_session, wait_for
    ):
        service = self._start_service(monkeypatch, scripted_llm)
        create = client.post(
            "/api/runs",
            json={"objective": ACTION_OBJECTIVE, "auto_approve": False},
            headers=auth_headers,
        )
        assert create.status_code == 202
        run_id = create.json()["id"]
        try:
            paused = wait_for(
                lambda: (
                    api_run(client, auth_headers, run_id)
                    if api_run(client, auth_headers, run_id)["status"] == "awaiting_approval"
                    else None
                ),
                timeout=25,
                message="run to pause for approval",
            )
            approval = paused["pending_approval"]
            assert approval

            decide = client.post(
                f"/api/runs/{run_id}/approvals/{approval['id']}",
                json={"decision": "rejected", "note": "Not this quarter"},
                headers=auth_headers,
            )
            assert decide.status_code == 200, decide.text

            final = wait_for(
                lambda: (
                    api_run(client, auth_headers, run_id)
                    if api_run(client, auth_headers, run_id)["status"]
                    in {"completed", "failed", "cancelled"}
                    else None
                ),
                timeout=40,
                message="run to finish after rejection",
            )
            assert final["status"] == "completed", final
            assert final["final_report"]

            tasks = task_rows(db_session, run_id)
            execution_task = next(task for task in tasks if task.agent == "execution")
            assert execution_task.status == "skipped"
            # The reporting task depends on the skipped execution task → also skipped.
            reporting_task = next(task for task in tasks if task.agent == "reporting")
            assert reporting_task.status == "skipped"

            events = event_rows(db_session, run_id)
            types = [event.type for event in events]
            assert "approval.resolved" in types
            resolved = [event for event in events if event.type == "approval.resolved"]
            assert resolved[0].data_json["decision"] == "rejected"
            assert "run.completed" in types
            # Rejected run still delivers an honest report with the limitation documented.
            assert any(
                "skipped" in (event.message or "").lower()
                for event in events
                if event.type == "agent.completed"
            )
        finally:
            join_service_threads(service, run_id)
