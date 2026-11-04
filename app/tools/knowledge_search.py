"""``knowledge_search`` — retrieval over the internal knowledge corpus (RAG).

Thin tool wrapper around :mod:`app.rag`: it embeds the query with the deterministic
local embedder, scores persisted ``knowledge_chunks`` by cosine + lexical hybrid,
and returns self-describing snippets (title, source, score) that agents can cite.
No network call is made.

An important guardrail: the local embedder's scores are *relative relevance*, not
calibrated probabilities. An agent that guesses a strict threshold (e.g.
``min_score=0.5``) would get zero passages and be tempted to invent sources, so an
agent-supplied threshold is capped at ``MAX_AGENT_MIN_SCORE`` (0.25) and never goes
below the configured ``RAG_MIN_SCORE`` floor.
"""

from __future__ import annotations

from typing import Any

from app.rag.store import get_knowledge_store
from app.tools._support import clamp_int
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolSpec

MAX_K = 20
DEFAULT_K = 5
MAX_AGENT_MIN_SCORE = 0.25


class KnowledgeSearchTool(BaseTool):
    spec = ToolSpec(
        name="knowledge_search",
        description=(
            "Search the AgentForge internal knowledge base (pricing policy, quality "
            "escalation, marketing playbook, product notes, support taxonomy, QBR and "
            "market notes) and return the most relevant passages with title, source and "
            "similarity score. Use it to ground root-cause analysis and recommendations "
            "in internal evidence instead of guessing. Leave min_score at its default; "
            "values above 0.25 are capped."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query.",
                },
                "k": {
                    "type": "integer",
                    "description": "Number of passages to return (1-20, default 5).",
                },
                "min_score": {
                    "type": "number",
                    "description": (
                        "Minimum hybrid similarity score. Defaults are tuned; do not set "
                        "above 0.25 (higher values are capped, because local scores are not "
                        "calibrated probabilities)."
                    ),
                },
            },
            "required": ["query"],
        },
        scopes=("read:knowledge",),
        sensitive=False,
        category="knowledge",
        timeout_seconds=30,
    )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult.failure("query must be a non-empty string")
        k = clamp_int(args.get("k"), DEFAULT_K, 1, MAX_K)
        configured_min = float(getattr(ctx.settings, "rag_min_score", 0.0) or 0.0)
        configured_min = max(0.0, min(1.0, configured_min))
        requested = args.get("min_score")
        if requested is None:
            requested = configured_min
        try:
            requested = max(0.0, min(1.0, float(requested)))
        except (TypeError, ValueError):
            return ToolResult.failure("min_score must be a number between 0 and 1")
        effective_min = max(configured_min, min(requested, MAX_AGENT_MIN_SCORE))

        store = (
            get_knowledge_store(ctx.db_session_factory)
            if ctx.db_session_factory is not None
            else get_knowledge_store()
        )
        hits = store.search(query, k=k, min_score=effective_min)
        results = [hit.to_dict() for hit in hits]
        output: dict[str, Any] = {
            "query": query,
            "results": results,
            "count": len(results),
            "k": k,
            "min_score": round(effective_min, 4),
            "requested_min_score": round(requested, 4),
            "min_score_capped": effective_min < requested,
        }
        if not results:
            output["note"] = (
                "No knowledge passages matched. Try a broader query or fewer/plain "
                "keywords (leave min_score at its default). If the store is empty, ingest "
                "the corpus with `python -m app.rag.cli ingest data/knowledge`."
            )
        return ToolResult.success(output, meta={"count": len(results), "k": k})


TOOL = KnowledgeSearchTool()

__all__ = ["KnowledgeSearchTool", "MAX_AGENT_MIN_SCORE", "TOOL"]
