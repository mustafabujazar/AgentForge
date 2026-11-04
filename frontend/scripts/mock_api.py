#!/usr/bin/env python3
"""Dev-only mock backend for visually previewing the AgentForge dashboard.

This is NOT used in production: it exists so the built SPA can be rendered and
reviewed without the real FastAPI service (and so the demo screenshots in the
README can be regenerated). It implements the subset of docs/API_CONTRACT.md the
frontend consumes, including a live SSE stream.

Usage:
    python scripts/mock_api.py            # serves dist/ + mock /api on 127.0.0.1:8123
    python scripts/mock_api.py --port 9000 --no-open
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

NOW = datetime.now(timezone.utc)


def iso(minutes_ago: float = 0) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def msg(text: str) -> str:
    return text


TASKS_SPEC = [
    {
        "task_id": "t1",
        "title": "Pull Q3 revenue and product performance",
        "description": "Query the demo sales database for Q3 revenue, units, returns and margins by product.",
        "agent": "analyst",
        "dependencies": [],
        "tools": ["sales_sql", "csv_query"],
        "sensitive": False,
        "success_criteria": ["Revenue reconciles to the Q3 ledger", "Returns are excluded from net revenue"],
    },
    {
        "task_id": "t2",
        "title": "Identify the three worst-performing products",
        "description": "Rank products by net revenue variance vs plan and isolate the bottom three.",
        "agent": "analyst",
        "dependencies": ["t1"],
        "tools": ["sales_sql"],
        "sensitive": False,
        "success_criteria": ["Ranking uses net revenue, not gross", "At least one causal signal per product"],
    },
    {
        "task_id": "t3",
        "title": "Diagnose root causes with internal memos",
        "description": "Search the knowledge base and support tickets to explain each product's decline.",
        "agent": "research",
        "dependencies": ["t2"],
        "tools": ["knowledge_search", "web_search"],
        "sensitive": False,
        "success_criteria": ["Every hypothesis cites a source", "Distinct causal story per product"],
    },
    {
        "task_id": "t4",
        "title": "Draft recommendations for each product",
        "description": "Turn the diagnosis into concrete pricing, quality and marketing recommendations.",
        "agent": "execution",
        "dependencies": ["t3"],
        "tools": ["knowledge_search"],
        "sensitive": False,
        "success_criteria": ["Recommendations are specific and owner-assigned"],
    },
    {
        "task_id": "t5",
        "title": "Synthesize the final executive report",
        "description": "Compose the final Markdown deliverable with findings, evidence and next steps.",
        "agent": "reporting",
        "dependencies": ["t3", "t4"],
        "tools": ["report_writer"],
        "sensitive": False,
        "success_criteria": ["Report answers the original objective", "All claims are traceable"],
    },
]

AGENTS = [
    {
        "name": "research",
        "role": "Research Agent",
        "description": "Evidence gathering across the internal corpus and the web; produces sourced findings.",
        "capabilities": ["web_search", "knowledge_retrieval", "source_triangulation"],
        "tools": ["web_search", "knowledge_search"],
    },
    {
        "name": "analyst",
        "role": "Analyst Agent",
        "description": "SQL + statistical analysis over the demo sales database; owns the numbers.",
        "capabilities": ["sql_analysis", "variance_analysis", "data_quality_checks"],
        "tools": ["sales_sql", "csv_query"],
    },
    {
        "name": "execution",
        "role": "Execution Agent",
        "description": "Turns analysis into governed actions; sensitive actions require human approval.",
        "capabilities": ["workflow_actions", "invoice_adjustment", "recommendation_drafting"],
        "tools": ["invoice_adjustment", "knowledge_search"],
    },
    {
        "name": "reporting",
        "role": "Reporting Agent",
        "description": "Synthesizes task results and evaluations into an executive-grade Markdown report.",
        "capabilities": ["markdown_composition", "executive_summary", "citation_management"],
        "tools": ["report_writer"],
    },
]

TOOLS = [
    {
        "name": "sales_sql",
        "description": "Read-only SQL over the demo sales database (products, sales, marketing, support).",
        "category": "data",
        "sensitive": False,
        "scopes": ["read:sales"],
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "SELECT statement"}},
            "required": ["query"],
        },
    },
    {
        "name": "csv_query",
        "description": "Run aggregate queries over a CSV artifact with a SQL-like filter DSL.",
        "category": "data",
        "sensitive": False,
        "scopes": ["read:artifacts"],
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "sql": {"type": "string"}},
            "required": ["path", "sql"],
        },
    },
    {
        "name": "knowledge_search",
        "description": "Hybrid vector + keyword retrieval over internal memos.",
        "category": "knowledge",
        "sensitive": False,
        "scopes": ["read:knowledge"],
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the public web for market and competitor signals.",
        "category": "research",
        "sensitive": False,
        "scopes": ["net:web"],
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "invoice_adjustment",
        "description": "Issue a corrective invoice adjustment. Writes to billing; gated on human approval.",
        "category": "billing",
        "sensitive": True,
        "scopes": ["write:billing", "approval:required"],
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}, "amount": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["account_id", "amount", "reason"],
        },
    },
    {
        "name": "report_writer",
        "description": "Persist a Markdown report as a downloadable artifact.",
        "category": "reporting",
        "sensitive": False,
        "scopes": ["write:artifacts"],
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "markdown": {"type": "string"}},
            "required": ["title", "markdown"],
        },
    },
]

REPORT = """# Q3 Sales Performance Review

## Executive summary

Q3 net revenue landed at **$4.18M**, missing plan by **6.2%**. Three products account for
**81% of the variance**: *Nimbus Sensor* (quality), *Atlas Hub* (discount dependency), and
*Vertex Dock* (weak marketing support).

| Product | Net revenue | vs plan | Returns | Primary signal |
|---|---:|---:|---:|---|
| Nimbus Sensor | $412K | -22.4% | 9.8% | Quality tickets up 3.1x |
| Atlas Hub | $688K | -14.1% | 2.2% | 41% average discount |
| Vertex Dock | $295K | -11.7% | 1.4% | Marketing spend down 62% |

> Net revenue excludes returns and is reconciled to the Q3 ledger; gross figures overstate
> performance by approximately 3.8 points.

## What happened

1. **Nimbus Sensor** — a firmware regression shipped in July drove a spike in support escalations
   (`support_tickets` severity `high`, +310% QoQ). Refund requests followed with a two-week lag.
2. **Atlas Hub** — discount depth increased from 18% to 41% while list price held flat, compressing
   contribution margin to 12%. Deals are not converting at the expected rate and return rates are
   stable, so this is a pricing execution problem rather than a product problem.
3. **Vertex Dock** — marketing spend collapsed after the June reallocation. The product has the
   strongest repeat-purchase rate in the portfolio, so this is a demand-generation gap.

## Recommendations

- **Nimbus Sensor:** freeze firmware rollout, ship a hotfix, and proactively contact affected
  accounts with a service credit. Owner: Quality. Target: 14 days.
- **Atlas Hub:** cap discretionary discounting at 25% and re-quote open pipeline. Owner: Sales Ops.
- **Vertex Dock:** restore 60% of the reallocated spend for 6 weeks and measure repeat-purchase
  lift. Owner: Marketing.

## Sources

- `sales.db` — Q3 revenue, returns, discount and marketing tables
- Internal memo: *Quality escalation process* (2026-07)
- Internal memo: *Pricing policy — enterprise tiers* (2026-06)
- Internal memo: *Marketing playbook* (2026-05)

```json
{"net_revenue": 4182000, "variance_pct": -6.2, "products_flagged": 3}
```
"""

EVENTS_COMPLETED = [
    ("run.created", None, None, msg("Run accepted and queued."), {"objective_chars": 168}),
    ("run.started", None, None, msg("Orchestrator started; planner selected."), None),
    ("plan.created", None, "t5", msg("Plan created with 5 tasks across 4 agents."), {"tasks": 5}),
    ("task.started", "analyst", "t1", msg("Executing task: Pull Q3 revenue and product performance"), None),
    ("tool.called", "analyst", "t1", msg("Called tool sales_sql"), {"duration_ms": 84}),
    ("agent.completed", "analyst", "t1", msg("Task 1 returned a reconciled revenue table."), {"rows": 214}),
    ("evaluation.completed", "analyst", "t1", msg("Evaluation score 0.94 (pass)."), {"score": 0.94, "issues": []}),
    ("task.started", "analyst", "t2", msg("Executing task: Identify the three worst-performing products"), None),
    ("tool.called", "analyst", "t2", msg("Called tool sales_sql"), {"duration_ms": 61}),
    ("agent.completed", "analyst", "t2", msg("Ranked products; bottom three identified."), None),
    ("evaluation.completed", "analyst", "t2", msg("Evaluation score 0.88 (pass)."), {"score": 0.88, "issues": ["Ranking should exclude one-off bulk orders"]}),
    ("task.retry", "analyst", "t2", msg("Retrying task 2 with evaluator feedback."), {"attempt": 2}),
    ("task.started", "research", "t3", msg("Executing task: Diagnose root causes with internal memos"), None),
    ("tool.called", "research", "t3", msg("Called tool knowledge_search"), {"query": "quality escalation process"}),
    ("tool.called", "research", "t3", msg("Called tool web_search"), {"query": "Nimbus Sensor firmware regression"}),
    ("agent.completed", "research", "t3", msg("Three distinct causal stories documented with sources."), None),
    ("evaluation.completed", "research", "t3", msg("Evaluation score 0.91 (pass)."), {"score": 0.91}),
    ("task.started", "execution", "t4", msg("Executing task: Draft recommendations for each product"), None),
    ("approval.required", None, "t4", msg("Sensitive tool invoice_adjustment requires approval."), {"tool": "invoice_adjustment"}),
    ("approval.resolved", None, "t4", msg("Sensitive action approved by demo operator."), {"decision": "approved"}),
    ("agent.completed", "execution", "t4", msg("Recommendations drafted and owner-assigned."), None),
    ("evaluation.completed", "execution", "t4", msg("Evaluation score 0.86 (pass)."), {"score": 0.86}),
    ("task.started", "reporting", "t5", msg("Executing task: Synthesize the final executive report"), None),
    ("agent.completed", "reporting", "t5", msg("Executive report synthesized."), None),
    ("evaluation.completed", "reporting", "t5", msg("Evaluation score 0.96 (pass)."), {"score": 0.96}),
    ("report.created", "reporting", None, msg("Final report persisted as artifact."), {"artifact": "q3-sales-report.md"}),
    ("run.completed", None, None, msg("Workflow completed in 48.2s."), {"retries": 1, "tokens": 18420}),
]

EVENTS_APPROVAL = [
    ("run.created", None, None, msg("Run accepted and queued."), None),
    ("run.started", None, None, msg("Orchestrator started; planner selected."), None),
    ("plan.created", None, None, msg("Plan created with 4 tasks."), {"tasks": 4}),
    ("task.started", "analyst", "t1", msg("Executing task: Reconcile Q3 revenue between ledger and CRM"), None),
    ("tool.called", "analyst", "t1", msg("Called tool sales_sql"), {"duration_ms": 92}),
    ("agent.completed", "analyst", "t1", msg("Variance traced to 12 enterprise accounts."), None),
    ("evaluation.completed", "analyst", "t1", msg("Evaluation score 0.9 (pass)."), {"score": 0.9}),
    ("task.started", "execution", "t2", msg("Executing task: Prepare invoice adjustments"), None),
    ("approval.required", None, "t2", msg("Sensitive tool invoice_adjustment requires approval."), {"tool": "invoice_adjustment", "accounts": 10}),
    ("run.awaiting_approval", None, None, msg("Run paused: waiting for human decision."), None),
]


def task_record(spec: dict, *, status: str, attempts: int, score: float | None, result: str) -> dict:
    return {
        "id": f"rec-{spec['task_id']}",
        "task_id": spec["task_id"],
        "title": spec["title"],
        "agent": spec["agent"],
        "status": status,
        "attempts": attempts,
        "result": result,
        "evaluation": (
            {"score": score, "passed": score >= 0.7, "issues": [], "feedback": "Meets the success criteria."}
            if score is not None
            else None
        ),
    }


def build_run(
    run_id: str,
    objective: str,
    status: str,
    *,
    task_statuses: list[str],
    task_scores: list[float | None],
    attempts: list[int],
    minutes_ago: float,
    final_report: str | None = None,
    error: str | None = None,
    pending_approval: dict | None = None,
    tokens: tuple[int, int, int, int] = (12400, 6020, 18420, 26),
    retries: int = 1,
) -> dict:
    specs = TASKS_SPEC if run_id != "demo-approval" else TASKS_SPEC[:4]
    tasks = [
        task_record(
            spec,
            status=task_statuses[index] if index < len(task_statuses) else "pending",
            attempts=attempts[index] if index < len(attempts) else 0,
            score=task_scores[index] if index < len(task_scores) else None,
            result=(
                f"Completed {spec['title'].lower()} using {spec['agent']} capability set. "
                "Output persisted to the run state for downstream tasks."
            ),
        )
        for index, spec in enumerate(specs)
    ]
    completed_at = iso(minutes_ago) if status in {"completed", "failed", "cancelled"} else None
    return {
        "id": run_id,
        "objective": objective,
        "status": status,
        "plan": {
            "objective": objective,
            "summary": "Diagnose the business question, ground it in internal data, quantify impact and produce an executive deliverable.",
            "tasks": specs,
            "success_criteria": [
                "Every finding is supported by a source or query result",
                "Numbers reconcile with the demo sales dataset",
                "Final report answers the original objective",
            ],
        },
        "final_report": final_report,
        "error": error,
        "created_at": iso(minutes_ago + 3),
        "started_at": iso(minutes_ago + 2.9),
        "completed_at": completed_at,
        "token_usage": {
            "prompt_tokens": tokens[0],
            "completion_tokens": tokens[1],
            "total_tokens": tokens[2],
            "calls": tokens[3],
        },
        "pending_approval": pending_approval,
        "retries": retries,
        "tasks": tasks,
        "artifacts": artifacts_for(run_id) if final_report else [],
    }


ARTIFACTS = {
    "demo-completed": [
        {
            "id": "art-report",
            "run_id": "demo-completed",
            "name": "q3-sales-report.md",
            "kind": "markdown",
            "path": "artifacts/demo-completed/q3-sales-report.md",
            "description": "Final synthesized executive report",
            "created_at": iso(4),
        },
        {
            "id": "art-evals",
            "run_id": "demo-completed",
            "name": "task-evaluations.json",
            "kind": "json",
            "path": "artifacts/demo-completed/task-evaluations.json",
            "description": "Per-task evaluator scores and issues",
            "created_at": iso(4),
        },
    ],
    "demo-approval": [],
    "demo-running": [],
    "demo-failed": [],
}


def artifacts_for(run_id: str) -> list[dict]:
    return ARTIFACTS.get(run_id, [])


RUNS = [
    build_run(
        "demo-completed",
        "Analyze our sales performance, identify the three worst-performing products, investigate why they're underperforming, and generate a report with recommendations.",
        "completed",
        task_statuses=["completed"] * 5,
        task_scores=[0.94, 0.88, 0.91, 0.86, 0.96],
        attempts=[1, 2, 1, 1, 1],
        minutes_ago=4,
        final_report=REPORT,
        tokens=(12400, 6020, 18420, 26),
        retries=1,
    ),
    build_run(
        "demo-approval",
        "Investigate the Q3 revenue variance between the sales database and the CRM, determine the affected enterprise accounts, and issue a corrective invoice adjustment for the top 10 accounts.",
        "awaiting_approval",
        task_statuses=["completed", "awaiting_approval", "pending", "pending"],
        task_scores=[0.9, None, None, None],
        attempts=[1, 1, 0, 0],
        minutes_ago=11,
        pending_approval={
            "id": "apr-7f3a",
            "task_id": "t2",
            "title": "Execute invoice adjustment for 10 enterprise accounts",
            "requested_action": "Call the sensitive tool invoice_adjustment to issue a $128,400 corrective credit across 10 enterprise accounts.",
            "reason": "The tool writes to the billing system and is marked sensitive. A human must confirm the amounts before execution.",
        },
        tokens=(8900, 3410, 12310, 16),
    ),
    build_run(
        "demo-running",
        "Research our three biggest competitors' latest pricing moves and summarize the implications for our pricing strategy.",
        "running",
        task_statuses=["completed", "running", "pending", "pending"],
        task_scores=[0.87, None, None, None],
        attempts=[1, 0, 0, 0],
        minutes_ago=1,
        tokens=(4300, 1780, 6080, 8),
    ),
    build_run(
        "demo-failed",
        "Build a cohort retention model for the self-serve segment and propose the top three experiments for next quarter.",
        "failed",
        task_statuses=["completed", "failed", "pending", "pending"],
        task_scores=[0.82, None, None, None],
        attempts=[1, 2, 0, 0],
        minutes_ago=64,
        error="LLMTimeoutError: provider groq timed out after 30s (task t2, attempt 3/3). Run marked failed; partial results retained.",
        tokens=(6700, 2100, 8940, 14),
    ),
]

RUN_EVENTS = {
    "demo-completed": EVENTS_COMPLETED,
    "demo-approval": EVENTS_APPROVAL,
    "demo-running": EVENTS_COMPLETED[:12],
    "demo-failed": EVENTS_COMPLETED[:7] + [("run.failed", None, None, msg("Run failed: LLM timeout."), {"error": "LLMTimeoutError"})],
}


def find_run(run_id: str) -> dict | None:
    for run in RUNS:
        if run["id"] == run_id:
            return run
    return None


def events_for(run_id: str) -> list[dict]:
    result = []
    for seq, (event_type, agent, task_id, message, data) in enumerate(RUN_EVENTS.get(run_id, []), start=1):
        result.append(
            {
                "seq": seq,
                "run_id": run_id,
                "type": event_type,
                "ts": iso(4 - seq * 0.02),
                "agent": agent,
                "task_id": task_id,
                "message": message,
                "data": data,
                "duration_ms": None if data is None else 40 + seq * 7,
            }
        )
    return result


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgentForgeMock/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ------------------------------------------------------------------ utils
    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str = "text/markdown", status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _not_found(self) -> None:
        self._send_json({"detail": "Not found"}, 404)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header.startswith("Bearer ")

    # ------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if not path.startswith("/api/"):
            self._serve_static(path)
            return

        if path == "/api/health":
            self._send_json(
                {
                    "status": "ok",
                    "version": "1.0.0",
                    "llm_provider": "mock",
                    "llm_model": "mock-1",
                    "database": "sqlite",
                }
            )
            return

        if not self._authorized():
            self._send_json({"detail": "Not authenticated"}, 401)
            return

        if path == "/api/auth/me":
            self._send_json(
                {
                    "id": "u-demo",
                    "email": "demo@agentforge.io",
                    "full_name": "Demo Operator",
                    "role": "admin",
                    "created_at": iso(60 * 24 * 30),
                }
            )
            return

        if path == "/api/runs":
            items = []
            for run in RUNS:
                item = {key: value for key, value in run.items() if key != "tasks"}
                items.append(item)
            self._send_json({"items": items, "total": len(items)})
            return

        if path == "/api/metrics/runs":
            self._send_json(
                {
                    "total_runs": 128,
                    "completed": 96,
                    "failed": 9,
                    "success_rate": 0.914,
                    "avg_duration_seconds": 47.3,
                    "total_tokens": 1_284_500,
                    "total_retries": 23,
                    "avg_eval_score": 0.874,
                    "by_status": {"completed": 96, "failed": 9, "running": 4, "cancelled": 19},
                }
            )
            return

        if path == "/api/agents":
            self._send_json({"agents": AGENTS})
            return

        if path == "/api/tools":
            self._send_json({"tools": TOOLS})
            return

        if path == "/api/knowledge/search":
            q = (query.get("q") or [""])[0]
            self._send_json(
                {
                    "results": [
                        {
                            "title": "Pricing policy — enterprise tiers",
                            "source": "data/knowledge/pricing_policy.md",
                            "text": "Enterprise accounts receive tiered discounts capped at 25% unless approved by Revenue Ops. Discounts above 30% require a margin review, and anything above 40% is treated as a policy exception.",
                            "score": 0.82,
                        },
                        {
                            "title": "Quality escalation process",
                            "source": "data/knowledge/quality_process.md",
                            "text": "SEV-1 quality regressions trigger a firmware rollout freeze within 24 hours, proactive customer outreach, and service credits for affected accounts.",
                            "score": 0.74,
                        },
                        {
                            "title": "Marketing playbook — demand generation",
                            "source": "data/knowledge/marketing_playbook.md",
                            "text": "Reallocating spend away from a product with a strong repeat-purchase rate should be reversible for at least four weeks to measure elasticity.",
                            "score": 0.61,
                        },
                    ][: int((query.get("k") or ["3"])[0]) if (query.get("q") or [""])[0] else 0],
                    "query": q,
                }
            )
            return

        if path.startswith("/api/artifacts/") and path.endswith("/download"):
            artifact_id = path.split("/")[3]
            if artifact_id == "art-report":
                self._send_text(REPORT)
            else:
                self._send_text('{"evaluations": []}', "application/json")
            return

        if path.startswith("/api/runs/"):
            parts = path.strip("/").split("/")
            run_id = parts[2] if len(parts) > 2 else ""
            run = find_run(run_id)
            if not run:
                self._not_found()
                return
            suffix = "/".join(parts[3:])
            if suffix == "":
                self._send_json(run)
                return
            if suffix == "tasks":
                self._send_json(run["tasks"])
                return
            if suffix == "events":
                after = int((query.get("after_seq") or ["0"])[0])
                self._send_json([event for event in events_for(run_id) if event["seq"] > after])
                return
            if suffix == "events/stream":
                self._stream_events(run_id)
                return
            if suffix == "report":
                if not run.get("final_report"):
                    self._send_json({"detail": "Report not ready"}, 404)
                    return
                self._send_json(
                    {
                        "run_id": run_id,
                        "report": run["final_report"],
                        "artifacts": artifacts_for(run_id),
                    }
                )
                return
            if suffix == "artifacts":
                self._send_json({"items": artifacts_for(run_id)})
                return
            self._not_found()
            return

        self._not_found()

    def _stream_events(self, run_id: str) -> None:
        events = events_for(run_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for event in events:
                frame = f"event: message\ndata: {json.dumps(event)}\n\n".encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()
                time.sleep(0.06)
            run = find_run(run_id)
            terminal = run and run["status"] in {"completed", "failed", "cancelled"}
            if terminal:
                # Give the client a final heartbeat then close, exercising the
                # polling fallback path gracefully.
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(0.2)
                return
            for _ in range(200):
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(3)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/api/auth/login":
            self._read_json()
            self._send_json(
                {
                    "access_token": "mock.jwt.token",
                    "token_type": "bearer",
                    "user": {
                        "id": "u-demo",
                        "email": "demo@agentforge.io",
                        "full_name": "Demo Operator",
                        "role": "admin",
                        "created_at": iso(60 * 24 * 30),
                    },
                }
            )
            return

        if not self._authorized():
            self._send_json({"detail": "Not authenticated"}, 401)
            return

        if path == "/api/runs":
            body = self._read_json()
            objective = body.get("objective", "Untitled objective")
            new_run = build_run(
                "demo-new",
                objective,
                "queued",
                task_statuses=["pending"] * 5,
                task_scores=[None] * 5,
                attempts=[0] * 5,
                minutes_ago=0,
                tokens=(0, 0, 0, 0),
            )
            self._send_json(new_run, 202)
            return

        if path.startswith("/api/runs/") and "/approvals/" in path:
            self._read_json()
            run = find_run("demo-approval")
            if not run:
                self._not_found()
                return
            run["status"] = "running"
            run["pending_approval"] = None
            self._send_json(run)
            return

        if path.startswith("/api/runs/") and path.endswith("/cancel"):
            run_id = path.strip("/").split("/")[2]
            run = find_run(run_id)
            if not run:
                self._not_found()
                return
            run["status"] = "cancelled"
            run["completed_at"] = iso(0)
            self._send_json(run)
            return

        self._not_found()

    # ---------------------------------------------------------------- static
    def _serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            path = "/index.html"
        candidate = (DIST / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(DIST.resolve())
        except ValueError:
            self._send_text("Forbidden", "text/plain", 403)
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.exists():
            # SPA-style fallback to index.html for unknown paths.
            candidate = DIST / "index.html"
        suffix = candidate.suffix.lower()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(suffix) or mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentForge dev mock backend")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()

    if not (DIST / "index.html").exists():
        sys.exit("dist/index.html missing — run `npm run build` first.")

    server = ThreadingHTTPServer((args.bind, args.port), MockHandler)
    print(f"[mock] serving {DIST} + mock /api at http://{args.bind}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
