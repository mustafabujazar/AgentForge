"""Command-line utilities for the knowledge store.

Examples::

    .\\venv\\Scripts\\python.exe -m app.rag.cli ingest data/knowledge
    .\\venv\\Scripts\\python.exe -m app.rag.cli search "Solstice chair returns" --k 5
    .\\venv\\Scripts\\python.exe -m app.rag.cli stats

Handy for demo setup and debugging retrieval without starting the API.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.rag.store import get_knowledge_store, reset_knowledge_store


def _cmd_ingest(args: argparse.Namespace) -> int:
    store = get_knowledge_store(force_reload=args.fresh)
    added = store.ingest_directory(Path(args.path))
    stats = store.stats()
    print(json.dumps({"chunks_added": added, **stats}, indent=2))
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    store = get_knowledge_store()
    hits = store.search(args.query, k=args.k, min_score=args.min_score)
    payload = [
        {
            "title": hit.title,
            "source": hit.source,
            "score": round(hit.score, 4),
            "text": hit.text[: args.preview],
        }
        for hit in hits
    ]
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload else 1


def _cmd_stats(args: argparse.Namespace) -> int:
    store = get_knowledge_store()
    print(json.dumps(store.stats(), indent=2))
    return 0


def _cmd_clear(args: argparse.Namespace) -> int:
    store = get_knowledge_store()
    deleted = store.clear()
    print(json.dumps({"chunks_deleted": deleted}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.rag.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="ingest a markdown directory")
    ingest.add_argument("path", nargs="?", default="data/knowledge")
    ingest.add_argument("--fresh", action="store_true", help="recreate the store singleton")
    ingest.set_defaults(func=_cmd_ingest)

    search = sub.add_parser("search", help="cosine search the knowledge store")
    search.add_argument("query")
    search.add_argument("--k", type=int, default=5)
    search.add_argument("--min-score", type=float, default=0.0)
    search.add_argument("--preview", type=int, default=300)
    search.set_defaults(func=_cmd_search)

    stats = sub.add_parser("stats", help="show document/chunk counts")
    stats.set_defaults(func=_cmd_stats)

    clear = sub.add_parser("clear", help="delete all stored chunks")
    clear.set_defaults(func=_cmd_clear)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
