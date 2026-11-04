#!/usr/bin/env python
"""AgentForge benchmark harness.

Runs the real workflow service (LangGraph graph + agents + tools + persistence)
over a fixed set of business objectives and reports measured metrics.

Modes
-----
* default (``--provider mock``): fully offline and deterministic.  A scripted
  ``BaseLLM`` replaces the provider, so *no network call is made*.  These runs
  measure orchestration correctness/overhead and produce reproducible numbers;
  the token counts are scripted word-count proxies and the cost is $0.00.  They
  are NOT a measure of model quality.
* ``--provider groq``: real hosted-model run (requires ``GROQ_API_KEY``).  Real
  results are marked as such in ``docs/BENCHMARK.md``.  Nothing is fabricated: if
  the real run is not executed, no real numbers are written.

Usage
-----
    .\\venv\\Scripts\\python.exe scripts/benchmark.py
    .\\venv\\Scripts\\python.exe scripts/benchmark.py --limit 2
    .\\venv\\Scripts\\python.exe scripts/benchmark.py --provider groq --limit 4 --cost-per-1m 0.25
    .\\venv\\Scripts\\python.exe scripts/benchmark.py --no-doc        # JSON only

Every invocation uses a throwaway SQLite database + artifacts directory, ingests
``data/knowledge`` into an isolated RAG store, and prints a JSON summary to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCS_DIR = PROJECT_ROOT / "docs"
DEFAULT_DOC = DOCS_DIR / "BENCHMARK.md"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

# --- Scenarios -------------------------------------------------------------
# ``approve`` scenarios exercise the HITL gate: the run pauses for approval and
# the harness records the human decision + resume latency as part of wall time.
SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "sales_performance_analysis",
        "objective": (
            "Analyze our sales performance, identify the three worst-performing products, "
            "investigate why they are underperforming, and generate a report with recommendations."
        ),
        "approve": False,
        "focus": "research -> SQL analysis -> action plan -> report",
    },
    {
        "name": "revenue_root_cause_diagnosis",
        "objective": (
            "Diagnose the root causes of the trailing-twelve-month revenue decline in our "
            "three weakest products and produce an evidence-backed executive briefing."
        ),
        "approve": False,
        "focus": "root-cause evidence + quantified deltas",
    },
    {
        "name": "remediation_plan_with_approval",
        "objective": (
            "Create a prioritized remediation plan for the three worst-performing products "
            "with owners, expected impact and risk."
        ),
        "approve": True,
        "focus": "sensitive execution task -> human approval -> resume",
    },
    {
        "name": "pricing_campaign_action",
        "objective": (
            "Launch a discount campaign: apply a 5% discount to the three worst-performing "
            "products, notify the sales team, and report the expected impact."
        ),
        "approve": True,
        "focus": "action-oriented objective + HITL approve -> execute -> report",
    },
]


# ===========================================================================
# Scripted offline LLM (mock provider)
# ===========================================================================
def _finding(statement: str, evidence: str, source: str, confidence: float = 0.8) -> dict[str, Any]:
    return {
        "statement": statement,
        "evidence": evidence,
        "source": source,
        "confidence": confidence,
    }


ACTION_PLAN_MD = """# Remediation Action Plan

## Actions

| # | Action | Owner | Expected Impact | Effort | Risk |
|---|---|---|---|---|---|
| 1 | Fix the 14-day quality escalation lag | Quality / Support | Recover ~8% of returned units | Medium | Low |
| 2 | Cap discretionary discounts at 20% | Sales Ops | +3pp gross margin | Low | Medium |
| 3 | Reallocate 15% of marketing spend to the launch product | Marketing | +12% units | Medium | Medium |
| 4 | Weekly underperformance review | Operations | Faster detection | Low | Low |

## Sensitive Steps (require human approval)

- Pricing and discount changes.
- Campaign launch and customer notifications.

## Limitations

Historical demo data only; validate with the owning teams.
"""

REPORT_MD = """# Business Analysis Report: Product Underperformance

## Executive Summary

Three products account for a disproportionate share of the revenue decline. The
analysis combines SQL evidence over the demo sales database with internal process
memos to explain why each product underperforms and what to do next.

## Key Findings & Evidence

- The portable audio line has the lowest trailing revenue at $138,613 and a
  3.26% return rate.
- The air purifier generated $337,552 in trailing revenue with a 29.06% average
  discount.
- The ergonomic chair generated $383,762 in trailing revenue and had a 12.36%
  return rate.

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
- Approve pricing and campaign actions through the HITL gate.
- Re-run the analysis after remediation to measure impact.
"""


def _build_scripted_llm_class():
    """Import ``BaseLLM`` lazily so environment setup happens before app imports."""
    from app.llm.base import BaseLLM, LLMResponse, TokenUsage

    class ScriptedBenchmarkLLM(BaseLLM):
        """Deterministic provider used by the offline benchmark (no network)."""

        provider = "mock"
        model = "benchmark-scripted"

        def __init__(self) -> None:
            self.calls = 0
            self.roles: list[str] = []

        def invoke(
            self,
            messages,
            *,
            temperature: float | None = None,
            max_tokens: int | None = None,
            json_mode: bool = False,
        ) -> "LLMResponse":
            started = time.perf_counter()
            self.calls += 1
            system = next((m.content for m in messages if getattr(m, "role", "") == "system"), "")
            user = next(
                (m.content for m in reversed(messages) if getattr(m, "role", "") == "user"), ""
            )
            role = self._detect_role(system)
            self.roles.append(role)
            if role == "planner":
                content = json.dumps(self._plan(user))
            elif role == "evaluator":
                content = json.dumps(
                    {
                        "score": 0.95,
                        "passed": True,
                        "criteria_met": ["answers task", "evidence backed"],
                        "issues": [],
                        "feedback": "Evidence-backed result, ready for the next agent.",
                    }
                )
            elif role == "synthesizer":
                content = REPORT_MD
            elif role in {"research", "analyst", "execution", "reporting"}:
                content = json.dumps(self._agent_step(role, messages))
            else:
                content = json.dumps({"thought": "unrecognised prompt", "action": "final", "result": {}})

            message_dicts = [
                {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}
                for m in messages
            ]
            prompt_tokens = sum(len(m["content"].split()) for m in message_dicts)
            completion_tokens = len(content.split())
            return LLMResponse(
                content=content,
                model=self.model,
                provider=self.provider,
                usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    model=self.model,
                    provider=self.provider,
                ),
                latency_ms=int((time.perf_counter() - started) * 1000),
                finish_reason="stop",
            )

        # -- helpers -------------------------------------------------------
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
        def _objective_from_prompt(user: str) -> str:
            marker = "OBJECTIVE:"
            if marker in user:
                return user.split(marker, 1)[1].split("Return the plan JSON", 1)[0].strip()
            first_line = user.strip().splitlines()[0] if user.strip() else "Business objective"
            return first_line[:300]

        def _plan(self, user: str) -> dict[str, Any]:
            objective = self._objective_from_prompt(user)
            return {
                "objective": objective,
                "summary": (
                    "Research root causes, quantify performance with SQL, draft a remediation "
                    "plan, then synthesize an executive business report."
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
                    "thought": "Search internal memos.",
                    "action": "tool",
                    "tool": "knowledge_search",
                    "args": {
                        "query": "quality escalation pricing policy marketing underperformance",
                        "k": 3,
                    },
                }
            if role == "analyst":
                return {
                    "thought": "Quantify revenue by product.",
                    "action": "tool",
                    "tool": "sales_sql",
                    "args": {
                        "query": (
                            "SELECT p.name, ROUND(SUM(s.revenue), 2) AS revenue, "
                            "ROUND(SUM(s.revenue - s.cost), 2) AS gross_profit, "
                            "SUM(s.quantity) AS units, "
                            "ROUND(SUM(CASE WHEN s.returned=1 THEN 1 ELSE 0 END)*100.0/"
                            "COUNT(s.sale_id),2) AS return_rate_pct, "
                            "ROUND(AVG(s.discount)*100,2) AS avg_discount_pct FROM sales s "
                            "JOIN products p ON p.product_id = s.product_id "
                            "WHERE s.sale_date >= date((SELECT MAX(sale_date) FROM sales), "
                            "'start of month', '-11 months') "
                            "GROUP BY p.product_id ORDER BY revenue ASC LIMIT 3"
                        )
                    },
                }
            if role == "execution":
                return {
                    "thought": "Persist the action plan.",
                    "action": "tool",
                    "tool": "report_writer",
                    "args": {
                        "title": "Remediation Action Plan",
                        "content": ACTION_PLAN_MD,
                        "name": "remediation-action-plan.md",
                        "description": "Prioritized remediation plan.",
                    },
                }
            return {
                "thought": "Persist the draft report.",
                "action": "tool",
                "tool": "report_writer",
                "args": {
                    "title": "Business Analysis Report",
                    "content": REPORT_MD,
                    "name": "business-analysis-report.md",
                    "description": "Full business analysis report.",
                },
            }

        def _agent_step(self, role: str, messages) -> dict[str, Any]:
            has_observation = any(
                getattr(m, "role", "") == "user" and "Observation from tool" in m.content
                for m in messages
            )
            if not has_observation:
                return self._tool_action(role)
            return {"thought": "Evidence collected.", "action": "final", "result": self._final_payload(role)}

        def _final_payload(self, role: str) -> dict[str, Any]:
            if role == "research":
                return {
                    "summary": (
                        "Internal memos explain the three underperformance stories: quality "
                        "escalation delays, discount dependency and weak marketing support."
                    ),
                    "findings": [
                        _finding(
                            "Quality escalation lags 14 days",
                            "The quality memo records a 14-day median defect escalation.",
                            "quality_escalation.md",
                            0.82,
                        ),
                        _finding(
                            "Discretionary discounts exceed policy",
                            "Pricing policy caps discounts at 20%; the chair averages 31%.",
                            "pricing_policy.md",
                            0.78,
                        ),
                        _finding(
                            "Launch products are under-marketed",
                            "Playbook requires 15% launch support; the air purifier received 6%.",
                            "marketing_playbook.md",
                            0.75,
                        ),
                    ],
                    "confidence": 0.82,
                }
            if role == "analyst":
                return {
                    "summary": (
                        "Trailing-twelve-month SQL analysis ranks portable audio last at $138,613, "
                        "then the air purifier at $337,552 and the ergonomic chair at $383,762."
                    ),
                    "findings": [
                        _finding(
                            "Portable audio has the lowest trailing revenue",
                            "$138,613 trailing revenue and a 3.26% return rate.",
                            "sales_sql: trailing-12m revenue by product",
                            0.9,
                        ),
                        _finding(
                            "Air purifier has a high average discount",
                            "$337,552 trailing revenue with a 29.06% average discount.",
                            "sales_sql: discount and revenue by product",
                            0.86,
                        ),
                        _finding(
                            "Ergonomic chair has the highest return rate",
                            "$383,762 trailing revenue and a 12.36% return rate.",
                            "sales_sql: return rate and revenue by product",
                            0.86,
                        ),
                    ],
                    "confidence": 0.88,
                }
            if role == "execution":
                return {
                    "summary": (
                        "Prioritized remediation plan with four owned actions, expected impact "
                        "and explicit sensitive-step flags."
                    ),
                    "findings": [
                        _finding(
                            "Quality escalation fix is the highest-impact action",
                            "Reducing escalation lag recovers roughly 8% of returned units.",
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
                        "Portable audio, chair and air purifier explain most of the drop.",
                        "analysis task-2",
                        0.85,
                    )
                ],
                "artifact_markdown": REPORT_MD,
                "confidence": 0.88,
            }

    return ScriptedBenchmarkLLM


# ===========================================================================
# Environment / lifecycle helpers
# ===========================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the AgentForge workflow benchmark.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--provider",
        default="mock",
        choices=("mock", "groq"),
        help="mock = offline scripted provider (default); groq = real hosted model run",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=len(SCENARIOS),
        help=f"maximum number of scenarios to run (1-{len(SCENARIOS)})",
    )
    parser.add_argument(
        "--cost-per-1m",
        type=float,
        default=0.0,
        help="optional blended USD cost per 1M tokens for the live provider estimate",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="per-scenario wall-clock timeout in seconds",
    )
    parser.add_argument(
        "--doc",
        type=Path,
        default=DEFAULT_DOC,
        help="markdown report output path",
    )
    parser.add_argument(
        "--no-doc",
        action="store_true",
        help="do not write the markdown report (print JSON only)",
    )
    return parser.parse_args(argv)


def prepare_environment(provider: str) -> tuple[Path, Path]:
    """Set env for an isolated run BEFORE any ``app.*`` import happens."""
    temp_root = Path(tempfile.mkdtemp(prefix="agentforge-benchmark-"))
    db_path = temp_root / "benchmark.db"
    artifacts_dir = temp_root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["LLM_PROVIDER"] = provider
    os.environ["ARTIFACTS_DIR"] = str(artifacts_dir)
    os.environ["KNOWLEDGE_DIR"] = str(PROJECT_ROOT / "data" / "knowledge")
    os.environ["SALES_DB_PATH"] = str(PROJECT_ROOT / "data" / "demo" / "sales.db")
    os.environ.setdefault("APP_ENV", "benchmark")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    os.environ.setdefault("LOG_JSON", "false")
    if provider == "mock":
        # The offline benchmark must never reach a provider, even if .env has a key.
        os.environ["GROQ_API_KEY"] = ""
    return temp_root, artifacts_dir


def ingest_demo_corpus() -> int:
    from app.rag.store import get_knowledge_store

    store = get_knowledge_store()
    added = store.ingest_directory(PROJECT_ROOT / "data" / "knowledge")
    return int(added or 0)


def decide_approval(run_id: str, decision: str = "approved") -> bool:
    """Apply a benchmark decision to the latest pending approval (simulated human)."""
    from app.db.base import session_scope
    from app.db.models import Approval, utcnow
    from app.orchestrator import persistence

    approval = persistence.latest_approval(run_id)
    if approval is None or (approval.status or "").strip().lower() != "pending":
        return False
    with session_scope() as session:
        row = session.get(Approval, approval.id)
        if row is None:
            return False
        row.status = decision
        row.note = "benchmark auto-decision"
        row.decided_at = utcnow()
        session.add(row)
    return True


def wait_terminal(run_id: str, timeout: float) -> str:
    from app.orchestrator import persistence

    deadline = time.monotonic() + max(1.0, timeout)
    status = persistence.run_status(run_id)[0]
    while status not in TERMINAL_STATUSES:
        if time.monotonic() > deadline:
            raise TimeoutError(f"run {run_id} did not finish within {timeout:.0f}s (status={status})")
        time.sleep(0.05)
        status = persistence.run_status(run_id)[0]
    return status


def collect_run_metrics(run_id: str, latency_seconds: float, resumed: bool) -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.db.base import session_scope
    from app.db.models import Artifact, EvaluationMetric, RunEvent, TaskRecord
    from app.orchestrator import persistence

    run = persistence.get_run(run_id)
    with session_scope() as session:
        task_rows = list(
            session.execute(select(TaskRecord).where(TaskRecord.run_id == run_id)).scalars()
        )
        evaluation_scores = [
            float(score)
            for score in session.execute(
                select(EvaluationMetric.score).where(EvaluationMetric.run_id == run_id)
            ).scalars()
            if score is not None
        ]
        artifact_count = int(
            session.execute(
                select(func.count()).select_from(Artifact).where(Artifact.run_id == run_id)
            ).scalar_one()
        )
        event_count = int(
            session.execute(
                select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)
            ).scalar_one()
        )

    statuses = [str(row.status or "") for row in task_rows]
    attempts = [int(row.attempts or 0) for row in task_rows]
    usage = dict(run.token_usage_json or {}) if run is not None else {}
    return {
        "run_id": run_id,
        "status": str(run.status) if run is not None else "missing",
        "error": (str(run.error)[:300] if run is not None and run.error else None),
        "latency_seconds": round(float(latency_seconds), 3),
        "tasks_total": len(task_rows),
        "tasks_completed": statuses.count("completed"),
        "tasks_failed": statuses.count("failed"),
        "tasks_skipped": statuses.count("skipped"),
        "max_attempts": max(attempts) if attempts else 0,
        "retries": sum(max(attempt - 1, 0) for attempt in attempts),
        "avg_eval_score": (
            round(sum(evaluation_scores) / len(evaluation_scores), 4)
            if evaluation_scores
            else None
        ),
        "evaluations": len(evaluation_scores),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "llm_calls": int(usage.get("calls") or 0),
        "artifacts": artifact_count,
        "events": event_count,
        "final_report_chars": len(run.final_report or "") if run is not None else 0,
        "resumed_after_approval": bool(resumed),
    }


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(ordered[low], 3)
    value = ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
    return round(float(value), 3)


# ===========================================================================
# Runner
# ===========================================================================
def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = SCENARIOS[: max(1, min(int(args.limit), len(SCENARIOS)))]

    # Provider + service are resolved after the environment is prepared.
    if args.provider == "mock":
        llm = _build_scripted_llm_class()()
        model = llm.model
    else:
        from app.core.errors import LLMConfigurationError
        from app.llm.registry import get_llm

        model = ""
        try:
            llm = get_llm("groq")
            model = getattr(llm, "model", "")
        except LLMConfigurationError as exc:  # clear, actionable failure
            raise SystemExit(
                f"cannot run --provider groq: {exc}\n"
                "Set GROQ_API_KEY in .env (or the environment) and retry."
            ) from exc

    from app.db.base import get_engine, init_db
    from app.orchestrator.service import WorkflowService

    init_db()
    chunks = ingest_demo_corpus()
    service = WorkflowService(llm=llm)

    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        started = time.perf_counter()
        run_id = asyncio.run(
            service.run_sync(scenario["objective"], auto_approve=not scenario["approve"])
        )
        resumed = False
        if scenario["approve"]:
            from app.orchestrator import persistence

            status = persistence.run_status(run_id)[0]
            if status == "awaiting_approval":
                if decide_approval(run_id, "approved"):
                    asyncio.run(service.resume_run(run_id))
                    resumed = True
            wait_terminal(run_id, args.timeout)
        latency = time.perf_counter() - started
        metrics = collect_run_metrics(run_id, latency, resumed)
        metrics.update(
            {
                "name": scenario["name"],
                "objective": scenario["objective"],
                "focus": scenario["focus"],
                "approval_gate": bool(scenario["approve"]),
            }
        )
        results.append(metrics)
        print(
            f"[benchmark] {scenario['name']}: status={metrics['status']} "
            f"latency={metrics['latency_seconds']}s tasks={metrics['tasks_completed']}/"
            f"{metrics['tasks_total']} tokens={metrics['total_tokens']} "
            f"retries={metrics['retries']}",
            file=sys.stderr,
        )

    total = len(results)
    completed = sum(1 for item in results if item["status"] == "completed")
    latencies = [item["latency_seconds"] for item in results]
    all_scores = [item["avg_eval_score"] for item in results if item["avg_eval_score"] is not None]
    total_tokens = sum(item["total_tokens"] for item in results)
    total_retries = sum(item["retries"] for item in results)
    cost_estimate = round(total_tokens / 1_000_000 * float(args.cost_per_1m), 6)

    get_engine().dispose()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": args.provider,
        "model": model,
        "mode": (
            "offline-scripted"
            if args.provider == "mock"
            else "live-provider (real API calls)"
        ),
        "environment": {
            "python": sys.version.split()[0],
            "database": "temporary sqlite (isolated)",
            "artifacts": "temporary directory",
            "knowledge_corpus_chunks": chunks,
            "scenarios_run": total,
        },
        "totals": {
            "runs_total": total,
            "runs_completed": completed,
            "success_rate": round(completed / total, 4) if total else 0.0,
            "wall_latency_p50_seconds": percentile(latencies, 50),
            "wall_latency_p95_seconds": percentile(latencies, 95),
            "avg_evaluation_score": (
                round(sum(all_scores) / len(all_scores), 4) if all_scores else None
            ),
            "retry_count": total_retries,
            "total_tokens": total_tokens,
            "cost_estimate_usd": cost_estimate if args.provider != "mock" else 0.0,
        },
        "scenarios": results,
        "notes": [
            (
                "Offline scripted-LLM benchmark: deterministic, no network. Latency measures "
                "orchestration + tools + DB, not model latency. Token counts are scripted "
                "word-count proxies; cost is $0.00 because no billed calls occur."
            )
            if args.provider == "mock"
            else (
                "LIVE provider run: metrics come from real hosted-model API calls. "
                "Cost is an estimate from --cost-per-1m and reported token usage."
            ),
        ],
    }


# ===========================================================================
# Markdown report
# ===========================================================================
def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(summary: dict[str, Any], args: argparse.Namespace) -> str:
    totals = summary["totals"]
    environment = summary["environment"]
    live = summary["provider"] != "mock"
    lines: list[str] = []
    lines.append("# AgentForge Benchmark")
    lines.append("")
    lines.append(f"_Generated: {summary['generated_at']}_")
    lines.append("")
    lines.append("## What this measures")
    lines.append("")
    lines.append(
        "The benchmark drives the **real** workflow (LangGraph graph, planner, four role "
        "agents, tool registry, evaluator/retry loop, HITL approval gate, synthesizer and "
        "SQLite persistence) over fixed business objectives. It measures: runs completed / "
        "total, success rate, wall-clock latency (p50/p95 across scenarios), average "
        "evaluation score, retry count, total tokens, artifact/event production and an "
        "optional token-cost estimate."
    )
    lines.append("")
    if live:
        lines.append(
            f"> **REAL PROVIDER RUN — these are actual API results** "
            f"(provider=`{summary['provider']}`, model=`{summary['model']}`). "
            "No numbers below are simulated."
        )
    else:
        lines.append(
            "> **OFFLINE SCRIPTED-LLM RUN — not a model-quality benchmark.** "
            "The LLM is replaced by a deterministic scripted provider "
            "(provider=`mock`, model=`benchmark-scripted`), so no network calls occur. "
            "These numbers measure orchestration/tool/persistence health and are "
            "reproducible on any machine; token counts are scripted word-count proxies and "
            "cost is $0.00. Real Groq numbers are never fabricated — run "
            "`python scripts/benchmark.py --provider groq` to produce them."
        )
    lines.append("")
    lines.append("## Environment")
    lines.append("")
    lines.append(f"- Python: `{environment['python']}`")
    lines.append(f"- Provider / model: `{summary['provider']}` / `{summary['model']}`")
    lines.append(f"- Mode: `{summary['mode']}`")
    lines.append(f"- Database: {environment['database']}")
    lines.append(f"- Artifacts: {environment['artifacts']}")
    lines.append(f"- Knowledge corpus chunks ingested: {environment['knowledge_corpus_chunks']}")
    lines.append(f"- Scenarios run: {environment['scenarios_run']} of {len(SCENARIOS)}")
    lines.append("")
    lines.append("## Results by scenario")
    lines.append("")
    lines.append(
        "| Scenario | Status | Wall latency (s) | Tasks completed/total | Skipped | "
        "Max attempts | Retries | Avg eval | Tokens | LLM calls | Artifacts | Report chars |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for item in summary["scenarios"]:
        lines.append(
            "| {name} | {status} | {latency} | {completed}/{total} | {skipped} | "
            "{attempts} | {retries} | {score} | {tokens} | {calls} | {artifacts} | {chars} |".format(
                name=item["name"],
                status=item["status"],
                latency=_fmt(item["latency_seconds"]),
                completed=item["tasks_completed"],
                total=item["tasks_total"],
                skipped=item["tasks_skipped"],
                attempts=item["max_attempts"],
                retries=item["retries"],
                score=_fmt(item["avg_eval_score"]),
                tokens=item["total_tokens"],
                calls=item["llm_calls"],
                artifacts=item["artifacts"],
                chars=item["final_report_chars"],
            )
        )
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Runs completed / total | {totals['runs_completed']} / {totals['runs_total']} |")
    lines.append(f"| Success rate | {_fmt(totals['success_rate'], 2)} |")
    lines.append(f"| Wall latency p50 | {_fmt(totals['wall_latency_p50_seconds'])} s |")
    lines.append(f"| Wall latency p95 | {_fmt(totals['wall_latency_p95_seconds'])} s |")
    lines.append(f"| Average evaluation score | {_fmt(totals['avg_evaluation_score'])} |")
    lines.append(f"| Retries (task attempts beyond the first) | {totals['retry_count']} |")
    lines.append(f"| Total tokens | {totals['total_tokens']} |")
    lines.append(f"| Cost estimate (USD) | {_fmt(totals['cost_estimate_usd'], 4)} |")
    lines.append("")
    gate_scenarios = [item for item in summary["scenarios"] if item["approval_gate"]]
    if gate_scenarios:
        resumed = sum(1 for item in gate_scenarios if item["resumed_after_approval"])
        lines.append(
            f"- HITL scenarios: {len(gate_scenarios)} approval-gated runs; "
            f"{resumed} paused and resumed after a recorded approval decision."
        )
        lines.append("")
    lines.append("## How to reproduce")
    lines.append("")
    lines.append("```powershell")
    lines.append("# offline deterministic (default; no network, no API key)")
    lines.append(".\\venv\\Scripts\\python.exe scripts/benchmark.py")
    lines.append("")
    lines.append("# limit the scenario count")
    lines.append(".\\venv\\Scripts\\python.exe scripts/benchmark.py --limit 2")
    lines.append("")
    lines.append("# real hosted-model run (requires GROQ_API_KEY in .env)")
    lines.append(".\\venv\\Scripts\\python.exe scripts/benchmark.py --provider groq --limit 4")
    lines.append("")
    lines.append("# JSON summary only, no markdown rewrite")
    lines.append(".\\venv\\Scripts\\python.exe scripts/benchmark.py --no-doc")
    lines.append("```")
    lines.append("")
    lines.append("Each invocation uses a throwaway SQLite database and artifacts directory "
                 "(the developer database is never touched) and ingests `data/knowledge` "
                 "into an isolated RAG store.")
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append(
        "- Offline mode replaces the LLM with a deterministic scripted provider: it verifies "
        "orchestration, tool execution, evaluation/retry plumbing and persistence, but says "
        "nothing about model quality or real-model latency."
    )
    lines.append(
        "- Latency p50/p95 are computed across the small scenario set (n="
        f"{len(summary['scenarios'])}), so they are indicative rather than statistical."
    )
    lines.append(
        "- The demo sales dataset and knowledge corpus are small; tool latency is not "
        "representative of production data volumes."
    )
    lines.append(
        "- Cost is only computed when `--cost-per-1m` is supplied; the default is 0.0 so no "
        "price is invented."
    )
    lines.append("")
    if not live:
        lines.append("## Real provider results")
        lines.append("")
        lines.append(
            "_Not executed in this environment._ No real Groq numbers are recorded here "
            "because none were measured. Running "
            "`python scripts/benchmark.py --provider groq --limit 4` writes its actual "
            "results into this file (clearly marked as a real provider run)."
        )
        lines.append("")
    if live:
        lines.append("## Reproducibility notes")
        lines.append("")
        lines.append(
            f"- This live run used `--provider {summary['provider']}` with model "
            f"`{summary['model']}` and `--cost-per-1m {args.cost_per_1m}`."
        )
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    temp_root, _artifacts_dir = prepare_environment(args.provider)
    exit_code = 0
    try:
        summary = run_benchmark(args)
        if not args.no_doc:
            args.doc.parent.mkdir(parents=True, exist_ok=True)
            args.doc.write_text(render_markdown(summary, args), encoding="utf-8")
            print(f"[benchmark] wrote {args.doc}", file=sys.stderr)
        print(json.dumps(summary, indent=2, default=str))
        totals = summary["totals"]
        if totals["runs_completed"] < totals["runs_total"]:
            exit_code = 2  # benchmark ran but not every run completed
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - top-level diagnostic
        print(f"[benchmark] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
    finally:
        try:
            from app.db.base import get_engine

            get_engine().dispose()
        except Exception:
            pass
        shutil.rmtree(temp_root, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
