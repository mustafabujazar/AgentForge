"""AgentForge orchestrator package.

The public entry point is :class:`app.orchestrator.service.WorkflowService`
(module-level singleton ``workflow_service``).  Everything else in this package is
internal to the workflow execution engine.

Sub-modules
-----------
- ``service``      : run lifecycle, background scheduling, DB persistence, resume.
- ``graph``        : LangGraph ``StateGraph`` (intake → plan → task loop → synthesize).
- ``planner``      : LLM planner with deterministic heuristic fallback.
- ``router``       : plan validation / agent capability routing / sensitive detection.
- ``executor``     : one bounded agent execution with timeout + cancellation.
- ``evaluator``    : deterministic rules + LLM judge, decides retry.
- ``approval``     : human-in-the-loop gate (LangGraph ``interrupt`` + Approval rows).
- ``synthesizer``  : final Markdown business report + artifact persistence.
- ``events``       : durable + live run event emission (monotonic seq per run).
- ``persistence``  : small transactional helpers over ``app.db``.
- ``runtime``      : per-run state shared by the graph nodes (LLM budget, cancel flag).
- ``llm_budget``   : per-run LLM call budget + token aggregation wrapper.
"""

from __future__ import annotations

__all__ = ["service", "graph", "planner", "router", "executor", "evaluator", "approval", "synthesizer"]
