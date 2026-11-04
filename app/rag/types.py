"""Public types for the AgentForge retrieval layer.

These are intentionally tiny, dependency-free dataclasses so that agents, the
API and tests can share them without importing SQLAlchemy or numpy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class Chunk:
    """One retrievable unit produced by the markdown-aware chunker."""

    title: str
    source: str
    text: str
    chunk_index: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RetrievedChunk:
    """A search hit returned by ``KnowledgeStore.search``.

    Mirrors the contract in docs/ARCHITECTURE.md §4.3:
    ``chunk_id, doc_id, title, source, text, score, metadata``.
    """

    chunk_id: str
    doc_id: str
    title: str
    source: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "title": self.title,
            "source": self.source,
            "text": self.text,
            "score": round(self.score, 4),
            "metadata": self.metadata,
        }


__all__ = ["Chunk", "RetrievedChunk"]
