"""Tolerant adapter between the HTTP API and the RAG layer.

``app/rag`` is owned by the tools/RAG engineer and is developed in parallel with
this API.  To keep the platform importable at every point in time, this module
resolves the retrieval facade lazily on first use and accepts either of the two
public shapes described in ``docs/ARCHITECTURE.md`` §4.3:

* module-level helpers in ``app.rag``::

      ingest_text(title, source, text, metadata=None) -> int
      ingest_directory(path) -> int
      search(query, k=5, min_score=0.0) -> list[RetrievedChunk]

* the knowledge-store singleton::

      from app.rag.store import get_knowledge_store
      store.ingest_text(...) / store.ingest_directory(...) / store.search(...)

Every failure surfaces as :class:`RAGUnavailable`, which the routers translate to
HTTP 503 so the UI degrades gracefully instead of crashing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["RAGUnavailable", "get_rag_bridge", "reset_rag_bridge"]


class RAGUnavailable(RuntimeError):
    """Raised when the retrieval layer is absent or cannot be initialized."""


class _StoreAdapter:
    """Adapts a ``KnowledgeStore`` instance to the module-level function API."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def ingest_text(
        self, title: str, source: str, text: str, metadata: dict[str, Any] | None = None
    ) -> int:
        method = getattr(self._store, "ingest_text", None)
        if not callable(method):
            raise RAGUnavailable("knowledge store does not implement ingest_text()")
        try:
            return _as_int(method(title, source, text, metadata=metadata or {}))
        except TypeError:
            return _as_int(method(title, source, text))

    def ingest_directory(self, path: Path | str) -> int:
        method = getattr(self._store, "ingest_directory", None)
        if not callable(method):
            raise RAGUnavailable("knowledge store does not implement ingest_directory()")
        return _as_int(method(path))

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        method = getattr(self._store, "search", None)
        if not callable(method):
            raise RAGUnavailable("knowledge store does not implement search()")
        try:
            hits = method(query, k=k)
        except TypeError:
            hits = method(query, k)
        return [_hit_to_dict(hit) for hit in (hits or [])]


class _ModuleAdapter:
    """Adapts module-level functions in ``app.rag``."""

    def __init__(self, module: Any) -> None:
        self._module = module

    def ingest_text(
        self, title: str, source: str, text: str, metadata: dict[str, Any] | None = None
    ) -> int:
        try:
            return _as_int(
                self._module.ingest_text(title, source, text, metadata=metadata or {})
            )
        except TypeError:
            return _as_int(self._module.ingest_text(title, source, text))

    def ingest_directory(self, path: Path | str) -> int:
        return _as_int(self._module.ingest_directory(path))

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        try:
            hits = self._module.search(query, k=k)
        except TypeError:
            hits = self._module.search(query, k)
        return [_hit_to_dict(hit) for hit in (hits or [])]


_adapter: _StoreAdapter | _ModuleAdapter | None = None


def get_rag_bridge() -> _StoreAdapter | _ModuleAdapter:
    """Return (and cache) the best available retrieval facade.

    Raises:
        RAGUnavailable: when ``app.rag`` is missing or exposes neither shape.
    """
    global _adapter
    if _adapter is not None:
        return _adapter
    try:
        import app.rag as rag  # noqa: F401  (may be a namespace package in progress)
    except Exception as exc:  # pragma: no cover - depends on parallel work
        raise RAGUnavailable(f"RAG layer is not available yet: {exc}") from exc

    if callable(getattr(rag, "ingest_text", None)) and callable(getattr(rag, "search", None)):
        _adapter = _ModuleAdapter(rag)
        logger.info("rag_bridge_resolved", extra={"shape": "module_functions"})
        return _adapter

    get_store = getattr(rag, "get_knowledge_store", None)
    if not callable(get_store):
        try:
            from app.rag.store import get_knowledge_store as get_store  # type: ignore[no-redef]
        except Exception:
            get_store = None
    if callable(get_store):
        try:
            store = get_store()
        except Exception as exc:  # pragma: no cover - depends on parallel work
            raise RAGUnavailable(f"knowledge store failed to initialize: {exc}") from exc
        if store is not None and callable(getattr(store, "search", None)):
            _adapter = _StoreAdapter(store)
            logger.info("rag_bridge_resolved", extra={"shape": "knowledge_store"})
            return _adapter

    raise RAGUnavailable(
        "RAG layer exposes no ingest_text/search API yet (app.rag is still being built)"
    )


def reset_rag_bridge() -> None:
    """Drop the cached facade (tests or after a late RAG module import)."""
    global _adapter
    _adapter = None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _hit_to_dict(hit: Any) -> dict[str, Any]:
    """Normalize a ``RetrievedChunk`` / dict / duck-typed hit to plain dict."""
    if isinstance(hit, dict):
        return {
            "title": str(hit.get("title") or ""),
            "source": str(hit.get("source") or ""),
            "text": str(hit.get("text") or ""),
            "score": _as_float(hit.get("score")),
            "metadata": hit.get("metadata") or {},
        }
    return {
        "title": str(getattr(hit, "title", "") or ""),
        "source": str(getattr(hit, "source", "") or ""),
        "text": str(getattr(hit, "text", "") or ""),
        "score": _as_float(getattr(hit, "score", 0.0)),
        "metadata": getattr(hit, "metadata", None) or {},
    }


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
