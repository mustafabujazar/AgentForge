#!/usr/bin/env python
"""AgentForge end-to-end demo runner (real acceptance test).

Usage::

    python scripts/demo_run.py
    python scripts/demo_run.py --objective "Analyze ..." --auto-approve
    python scripts/demo_run.py --provider groq
    python scripts/demo_run.py --provider mock --objective "..."

This initializes the database schema, ingests the knowledge corpus into the local
RAG store (idempotent), runs the full LangGraph workflow synchronously and prints:

* run id, status, duration
* token usage (prompt/completion/total/calls)
* the final report path + full Markdown
* any pending approval (for sensitive objectives without --auto-approve)

Exit codes: 0 = all tasks passed (or paused awaiting approval), 1 = failed, cancelled
or partial run, 2 = provider configuration error.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_OBJECTIVE = (
    "Analyze our sales performance, identify the three worst-performing products, "
    "investigate why they're underperforming, and generate a report with recommendations."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the AgentForge multi-agent workflow end-to-end.",
    )
    parser.add_argument(
        "--objective",
        "-o",
        default=DEFAULT_OBJECTIVE,
        help="Business objective for the multi-agent workflow.",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve sensitive action tasks (skip the HITL pause).",
    )
    parser.add_argument(
        "--provider",
        choices=["groq", "mock", "openai", "ollama"],
        default=None,
        help="Override LLM_PROVIDER for this run (default: from .env).",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Do not (re)ingest data/knowledge into the RAG store before running.",
    )
    parser.add_argument(
        "--report-chars",
        type=int,
        default=0,
        help="Only print the first N report characters (0 = print all).",
    )
    return parser.parse_args(argv)


def _ingest_knowledge(settings) -> int:
    """Best-effort idempotent ingestion of the internal knowledge corpus."""
    try:
        from app.rag import ingest_directory

        added = ingest_directory(settings.knowledge_path)
        print(f"[setup] knowledge corpus ingested: {added} new chunks")
        return added
    except Exception as exc:  # tools-rag layer may not be importable in isolation
        print(f"[setup] knowledge ingestion skipped: {type(exc).__name__}: {exc}")
        return 0


def _print_run(run_id: str, *, auto_approve: bool, report_chars: int) -> int:
    from app.db.base import session_scope
    from app.db.models import Artifact, Run, TaskRecord, EvaluationMetric
    from sqlalchemy import select

    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:  # pragma: no cover - should never happen
            print(f"[error] run {run_id} not found")
            return 1
        artifacts = list(
            session.execute(select(Artifact).where(Artifact.run_id == run_id)).scalars()
        )
        tasks = list(
            session.execute(select(TaskRecord).where(TaskRecord.run_id == run_id)).scalars()
        )
        metrics = list(
            session.execute(
                select(EvaluationMetric).where(EvaluationMetric.run_id == run_id)
            ).scalars()
        )
        status = run.status
        error = run.error
        report = run.final_report or ""
        plan = run.plan_json or {}
        usage = dict(run.token_usage_json or {})
        started_at = run.started_at
        completed_at = run.completed_at
        run_objective = run.objective

    report_artifact = next(
        (
            artifact
            for artifact in artifacts
            if artifact.description == "Final synthesized business report"
        ),
        None,
    )
    duration = (
        (completed_at - started_at).total_seconds() if completed_at and started_at else None
    )

    print("=" * 78)
    print("AgentForge demo run")
    print("=" * 78)
    print(f"run id        : {run_id}")
    print(f"status        : {status}")
    print(f"objective     : {run_objective}")
    print(f"duration      : {duration:.1f}s" if duration is not None else "duration      : n/a")
    print(
        "tokens        : "
        f"prompt={usage.get('prompt_tokens', 0)} "
        f"completion={usage.get('completion_tokens', 0)} "
        f"total={usage.get('total_tokens', 0)} calls={usage.get('calls', 0)}"
    )
    print(f"model         : {usage.get('model') or 'n/a'} ({usage.get('provider') or 'n/a'})")
    if error:
        print(f"error         : {error}")
    if plan.get("tasks"):
        print(f"plan          : {len(plan['tasks'])} tasks — " + ", ".join(
            f"{task.get('id')}:{task.get('agent')}" for task in plan["tasks"]
        ))
    failure_lines: list[str] = []
    for task in tasks:
        evaluation = task.evaluation_json or {}
        result = task.result_json or {}
        task_error = result.get("error") or ""
        print(
            f"  - {task.task_id:<8} {task.agent:<9} {task.status:<10} "
            f"attempts={task.attempts} eval={evaluation.get('score', 'n/a')}"
            + (f"  error={str(task_error)[:140]}" if task_error else "")
        )
        if task.status in {"failed", "skipped"}:
            detail = task_error or "; ".join(evaluation.get("issues") or []) or "no detail recorded"
            failure_lines.append(f"{task.task_id} ({task.agent}): {str(detail)[:300]}")
    if metrics:
        avg = sum(metric.score for metric in metrics) / len(metrics)
        print(f"eval metrics  : {len(metrics)} rows, avg score {avg:.3f}")
    if status == "completed":
        completed_count = sum(1 for task in tasks if task.status == "completed")
        print(f"tasks         : {completed_count}/{len(tasks)} completed")
        if failure_lines:
            print("partial success: some tasks failed or were skipped:")
            for line in failure_lines[:6]:
                print(f"  ! {line}")
    if status == "failed":
        print()
        print(f"FAILURE reason: {error or 'unknown'}")
        for line in failure_lines[:6]:
            print(f"  ! {line}")
    print()
    if status == "awaiting_approval":
        print(
            "[pause] This run is paused for human approval. Approve it via the API "
            "(POST /api/runs/{id}/approvals/{approval_id}) — the workflow will resume. "
            "Re-run with --auto-approve to skip the gate."
        )
        print("=" * 78)
        return 0
    if report_artifact is not None:
        print(f"report path   : {report_artifact.path}")
        print(f"report artifact: {report_artifact.name} ({len(report)} chars)")
        print("-" * 78)
        if not report:
            print("[no final_report attached — the run did not complete successfully]")
        elif report_chars and report_chars > 0:
            print(report[:report_chars])
            print(f"... [{len(report) - report_chars} more chars]")
        else:
            print(report)
        print("=" * 78)
    else:
        print("report        : (no artifact was written)")
    return 0 if status == "completed" and not failure_lines else 1


def _configure_stdio() -> None:
    """Force UTF-8 console output (reports contain unicode punctuation on Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - non-reconfigurable streams
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider

    # Import after env overrides so settings/LLM provider reflect them.
    from app.core.config import reload_settings

    settings = reload_settings()

    if settings.llm_provider == "groq" and not settings.groq_api_key:
        print(
            "[error] LLM_PROVIDER=groq but GROQ_API_KEY is not set.\n"
            "        Set it in .env or run with --provider mock.",
            file=sys.stderr,
        )
        return 2

    from app.db.base import init_db

    init_db()
    print(f"[setup] database ready: {settings.database_url}")
    print(
        f"[setup] provider={settings.llm_provider} model={settings.llm_model} "
        f"hitl={settings.hitl_enabled} auto_approve={args.auto_approve}"
    )
    if not args.skip_ingest:
        _ingest_knowledge(settings)

    from app.orchestrator.service import workflow_service

    started = time.perf_counter()
    try:
        run_id = asyncio.run(
            workflow_service.run_sync(
                args.objective,
                user_id=None,
                auto_approve=args.auto_approve,
            )
        )
    except Exception as exc:  # pragma: no cover - top-level safety net
        print(f"[error] workflow crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - started
    print(f"[run] finished in {elapsed:.1f}s wall clock")
    return _print_run(run_id, auto_approve=args.auto_approve, report_chars=args.report_chars)


if __name__ == "__main__":
    raise SystemExit(main())
