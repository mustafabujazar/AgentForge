"""``web_search`` — offline-safe "external market" search.

The demo runs with no network guarantee, so this tool **never** performs a live
request. Instead it searches the curated market-note corpus shipped in
``data/knowledge/market/*.md`` with the same deterministic local embeddings used
for RAG, and marks every result as ``simulated: true`` so agents can never confuse
it with live web data. If the corpus is missing, it returns a clearly-labelled
simulated placeholder list rather than failing or hanging.

The corpus is date-aligned with the demo dataset (May 2025 → October 2026) and is
deliberately **product-agnostic**: it describes categories and benchmarks, never a
specific internal SKU or SKU id. Responses advertise this via ``simulated``,
``network``, ``corpus_period`` and ``as_of`` so research agents cannot present
external category notes as internal product facts or invent dates/categories.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.rag.embeddings import LocalHashEmbedder, cosine_similarity
from app.tools._support import clamp_int, resolve_market_dir, slugify
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolSpec

MAX_RESULTS = 8
DEFAULT_RESULTS = 3
# Keep the external corpus date-consistent with the demo sales window.
CORPUS_PERIOD = "2025-2026"
CORPUS_AS_OF = "2026-10"
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_snippet(text: str, limit: int = 320) -> str:
    collapsed = _WHITESPACE_RE.sub(" ", text).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _load_market_docs(directory: Path) -> list[dict[str, str]]:
    docs: list[dict[str, str]] = []
    if not directory.is_dir():
        return docs
    for path in sorted(directory.glob("*.md"), key=lambda item: item.name):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title_match = _H1_RE.search(text)
        title = title_match.group(1).strip() if title_match else path.stem.replace("_", " ").title()
        stem = path.stem
        docs.append(
            {
                "title": title,
                "url": f"https://market-notes.agentforge.local/{slugify(stem)}",
                "source": path.name,
                "text": text,
                "body": _H1_RE.sub("", text),
            }
        )
    return docs


class WebSearchTool(BaseTool):
    spec = ToolSpec(
        name="web_search",
        description=(
            "Look up external market context (category benchmarks, competitor moves, "
            "pricing trends) from a curated offline market-note corpus covering 2025-2026. "
            "Results are SIMULATED — they are never live web results and are clearly "
            "marked simulated=true. They are category-level external benchmarks, never "
            "internal product facts; do not attribute them to a specific internal SKU and "
            "do not cite dates outside 2025-2026."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Market research question or keywords.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (1-8, default 3).",
                },
            },
            "required": ["query"],
        },
        scopes=("read:web",),
        sensitive=False,
        category="web",
        timeout_seconds=15,
    )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult.failure("query must be a non-empty string")
        max_results = clamp_int(args.get("max_results"), DEFAULT_RESULTS, 1, MAX_RESULTS)
        market_dir = resolve_market_dir(ctx)
        docs = _load_market_docs(market_dir)

        if not docs:
            return ToolResult.success(
                {
                    "query": query,
                    "results": [],
                    "count": 0,
                    "simulated": True,
                    "network": False,
                    "corpus_period": CORPUS_PERIOD,
                    "as_of": CORPUS_AS_OF,
                    "note": (
                        f"Offline market corpus not found at {market_dir}; run "
                        "scripts/generate_demo_data.py. No live web access is attempted."
                    ),
                },
                meta={"simulated": True, "count": 0},
            )

        embedder = LocalHashEmbedder(dim=192)
        embedder.fit([f"{doc['title']}\n{doc['text']}" for doc in docs])
        query_vector = embedder.embed(query)
        scored: list[tuple[float, dict[str, str]]] = []
        for doc in docs:
            vector = embedder.embed(f"{doc['title']}\n{doc['text']}")
            score = cosine_similarity(query_vector, vector)
            scored.append((score, doc))
        scored.sort(key=lambda item: item[0], reverse=True)

        results: list[dict[str, Any]] = []
        for score, doc in scored[:max_results]:
            results.append(
                {
                    "title": doc["title"],
                    "url": doc["url"],
                    "source": doc["source"],
                    "snippet": _clean_snippet(doc["body"]),
                    "score": round(float(score), 4),
                    "simulated": True,
                    "period": CORPUS_PERIOD,
                }
            )
        return ToolResult.success(
            {
                "query": query,
                "results": results,
                "count": len(results),
                "simulated": True,
                "network": False,
                "corpus": "data/knowledge/market",
                "corpus_period": CORPUS_PERIOD,
                "as_of": CORPUS_AS_OF,
                "note": (
                    "Curated offline market notes covering 2025-2026 only, not live web "
                    "search. External category benchmarks only: do not attribute them to a "
                    "specific internal SKU or cite dates outside 2025-2026. Internal facts "
                    "must come from knowledge_search or sales_sql."
                ),
            },
            meta={"simulated": True, "count": len(results)},
        )


TOOL = WebSearchTool()

__all__ = ["CORPUS_AS_OF", "CORPUS_PERIOD", "WebSearchTool", "TOOL"]
