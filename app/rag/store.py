"""Persistent knowledge store: chunking + embeddings + cosine search.

The store is intentionally database-portable.  Vectors live in the
``knowledge_chunks.embedding_json`` JSON column defined in ``app/db/models.py``;
no pgvector extension is required (the production pgvector path is documented in
``docs/rag.md`` as an upgrade, not a dependency).

Public API (see docs/ARCHITECTURE.md §4.3)::

    store = get_knowledge_store()
    store.ingest_text(title, source, text, metadata=None) -> int
    store.ingest_directory(path) -> int
    store.search(query, k=5, min_score=0.0) -> list[RetrievedChunk]

The store is idempotent: a document is keyed by ``sha1(source|title)``; re-ingesting
unchanged text is a no-op (returns 0), while changed text replaces that document's
chunks.  This makes startup seeding safe to call on every boot.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from app.db.models import KnowledgeChunk
from app.rag.chunking import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, chunk_markdown
from app.rag.embeddings import Embedder, LocalHashEmbedder, cosine_similarity
from app.rag.types import Chunk, RetrievedChunk

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]
DEFAULT_DIM = 384
DEFAULT_LEXICAL_WEIGHT = 0.22
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def stable_doc_id(source: str, title: str) -> str:
    """Deterministic short document id from its logical identity."""
    digest = hashlib.sha1(f"{source}|{title}".encode("utf-8")).hexdigest()
    return digest[:16]


def extract_title(markdown_text: str, fallback: str) -> str:
    """Return the first markdown H1, else a humanized fallback."""
    match = _H1_RE.search(markdown_text)
    if match:
        return match.group(1).strip()
    stem = fallback.rsplit("/", 1)[-1]
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    return stem.replace("_", " ").replace("-", " ").title()


def _embedding_input(title: str, text: str) -> str:
    """Text used for vectorization: a doubled title gives a small, useful boost."""
    if title:
        return f"{title}\n{title}\n{text}"
    return text


def _rows_signature(rows: Sequence[KnowledgeChunk]) -> str:
    fingerprint = "|".join(
        f"{row.doc_id}:{row.chunk_index}:{len(row.text)}" for row in rows
    )
    return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class KnowledgeStore:
    """SQLAlchemy-backed vector store with chunking and hashed TF-IDF embeddings."""

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        embedder: Embedder | None = None,
        dim: int = DEFAULT_DIM,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_OVERLAP,
    ) -> None:
        self._session_factory = session_factory
        self._embedder: Embedder = embedder or LocalHashEmbedder(dim)
        self.chunk_size = int(chunk_size)
        self.overlap = int(overlap)
        self._lock = threading.RLock()
        self._embedder_fitted = False
        self._corpus_signature: str | None = None
        self._schema_ready = False

    # -- infrastructure -------------------------------------------------
    @property
    def embedder(self) -> Embedder:
        return self._embedder

    @property
    def dim(self) -> int:
        return self._embedder.dim

    def _factory(self) -> SessionFactory:
        if self._session_factory is not None:
            return self._session_factory
        # Imported lazily so importing app.rag never builds a DB engine.
        from app.db.base import get_session_factory

        return get_session_factory()

    def _ensure_schema(self) -> None:
        """Create the schema for the default (app) database on first use."""
        if self._session_factory is not None or self._schema_ready:
            return
        from app.db.base import init_db

        init_db()
        self._schema_ready = True

    def _open(self):
        self._ensure_schema()
        return self._factory()()

    # -- ingest ---------------------------------------------------------
    def ingest_text(
        self,
        title: str,
        source: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Chunk, embed and persist one document. Returns chunks added (0 if unchanged)."""
        return self._ingest_many([(title, source, text, metadata)])

    def ingest_file(self, path: str | Path, *, root: str | Path | None = None) -> int:
        """Ingest one markdown file (source is its path relative to ``root``)."""
        file_path = Path(path)
        source = self._relative_source(file_path, Path(root) if root else file_path.parent)
        text = file_path.read_text(encoding="utf-8", errors="replace")
        title = extract_title(text, file_path.name)
        return self.ingest_text(title, source, text, metadata={"file": source})

    def ingest_directory(
        self,
        path: str | Path,
        *,
        pattern: str = "**/*.md",
    ) -> int:
        """Ingest every markdown file under ``path`` (sorted for determinism)."""
        target = Path(path)
        if not target.exists():
            logger.warning("knowledge_directory_missing", extra={"path": str(target)})
            return 0
        if target.is_file():
            return self.ingest_file(target)
        root = target
        files = sorted(
            (candidate for candidate in root.glob(pattern) if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(root).as_posix(),
        )
        documents: list[tuple[str, str, str, dict[str, Any] | None]] = []
        for file_path in files:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            source = file_path.relative_to(root).as_posix()
            title = extract_title(text, source)
            documents.append((title, source, text, {"file": source}))
        added = self._ingest_many(documents)
        logger.info(
            "knowledge_directory_ingested",
            extra={"path": str(root), "files": len(files), "chunks_added": added},
        )
        return added

    def _ingest_many(
        self,
        documents: Sequence[tuple[str, str, str, dict[str, Any] | None]],
    ) -> int:
        with self._lock:
            prepared: list[tuple[str, str, str, list[Chunk], dict[str, Any]]] = []
            for title, source, text, metadata in documents:
                title = (title or "").strip() or extract_title(text or "", source)
                chunks = chunk_markdown(
                    text or "",
                    title=title,
                    source=source,
                    chunk_size=self.chunk_size,
                    overlap=self.overlap,
                )
                if not chunks:
                    continue
                doc_id = stable_doc_id(source, title)
                prepared.append((doc_id, title, source, chunks, dict(metadata or {})))
            if not prepared:
                return 0

            session = self._open()
            try:
                existing = list(
                    session.execute(
                        select(KnowledgeChunk).order_by(KnowledgeChunk.doc_id, KnowledgeChunk.chunk_index)
                    ).scalars()
                )
                rows_by_doc: dict[str, list[KnowledgeChunk]] = {}
                for row in existing:
                    rows_by_doc.setdefault(row.doc_id, []).append(row)

                changed = [
                    item
                    for item in prepared
                    if [row.text for row in rows_by_doc.get(item[0], [])]
                    != [chunk.text for chunk in item[3]]
                ]
                if not changed:
                    logger.debug(
                        "knowledge_ingest_unchanged",
                        extra={"documents": len(prepared)},
                    )
                    return 0

                changed_ids = {item[0] for item in changed}
                kept = [row for row in existing if row.doc_id not in changed_ids]

                # Refit IDF on the full post-ingest corpus, then re-embed kept rows
                # because every persisted vector depends on corpus-wide IDF.
                corpus_texts = [_embedding_input(row.title, row.text) for row in kept]
                vectors_by_doc: dict[str, list[list[float]]] = {}
                for doc_id, title, _source, chunks, _metadata in changed:
                    vectors = [self._embedder.embed(_embedding_input(title, chunk.text)) for chunk in chunks]
                    vectors_by_doc[doc_id] = vectors
                    corpus_texts.extend(
                        _embedding_input(title, chunk.text) for chunk in chunks
                    )
                self._embedder.fit(corpus_texts)
                self._embedder_fitted = True
                for row in kept:
                    row.embedding_json = self._embedder.embed(
                        _embedding_input(row.title, row.text)
                    )

                if changed_ids:
                    session.execute(
                        delete(KnowledgeChunk).where(KnowledgeChunk.doc_id.in_(changed_ids))
                    )

                added = 0
                for doc_id, title, source, chunks, metadata in changed:
                    vectors = vectors_by_doc[doc_id]
                    for chunk, vector in zip(chunks, vectors):
                        chunk_metadata = dict(metadata)
                        chunk_metadata.update(chunk.metadata)
                        chunk_metadata.setdefault("headings", [])
                        session.add(
                            KnowledgeChunk(
                                doc_id=doc_id,
                                chunk_index=chunk.chunk_index,
                                title=title,
                                source=source,
                                text=chunk.text,
                                embedding_json=vector,
                                metadata_json=chunk_metadata,
                            )
                        )
                        added += 1
                session.commit()
                self._corpus_signature = None
                logger.info(
                    "knowledge_ingested",
                    extra={
                        "documents_changed": len(changed),
                        "documents_total": len(prepared),
                        "chunks_added": added,
                    },
                )
                return added
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    # -- query ----------------------------------------------------------
    def _load_rows(self, session) -> list[KnowledgeChunk]:
        return list(
            session.execute(
                select(KnowledgeChunk).order_by(KnowledgeChunk.doc_id, KnowledgeChunk.chunk_index)
            ).scalars()
        )

    def _ensure_fitted(self, rows: Sequence[KnowledgeChunk]) -> None:
        if not rows:
            return
        signature = _rows_signature(rows)
        if self._embedder_fitted and signature == self._corpus_signature:
            return
        self._embedder.fit(
            [_embedding_input(row.title, row.text) for row in rows]
        )
        self._embedder_fitted = True
        self._corpus_signature = signature

    def search(
        self,
        query: str,
        k: int = 5,
        min_score: float = 0.0,
    ) -> list[RetrievedChunk]:
        """Hybrid retrieval: hashed-TF-IDF cosine + light IDF lexical rerank.

        The primary signal is cosine similarity between the query embedding and the
        persisted chunk embedding (the contract in docs/ARCHITECTURE.md §4.3). A
        small lexical term-overlap component (22% weight) rewards exact rare terms
        such as product names and SKU ids; the blend is still deterministic and
        fully local. Component scores are exposed in ``metadata`` for debuggability.
        """
        if not query or not query.strip():
            return []
        k = max(1, int(k))
        with self._lock:
            session = self._open()
            try:
                rows = self._load_rows(session)
            finally:
                session.close()
            if not rows:
                return []
            self._ensure_fitted(rows)
            query_text = query.strip()
            query_vector = self._embedder.embed(query_text)
            lexical_fn = getattr(self._embedder, "binary_similarity", None)
            scored: list[tuple[float, float, float, KnowledgeChunk]] = []
            for row in rows:
                vector = row.embedding_json
                if not isinstance(vector, list) or not vector:
                    continue
                cosine = cosine_similarity(query_vector, vector)
                lexical = 0.0
                if callable(lexical_fn):
                    lexical = float(
                        lexical_fn(query_text, _embedding_input(row.title, row.text))
                    )
                score = (1.0 - DEFAULT_LEXICAL_WEIGHT) * cosine + DEFAULT_LEXICAL_WEIGHT * lexical
                if score >= min_score:
                    scored.append((score, cosine, lexical, row))
            scored.sort(key=lambda item: item[0], reverse=True)
            results: list[RetrievedChunk] = []
            for score, cosine, lexical, row in scored[:k]:
                metadata = dict(row.metadata_json or {})
                metadata.setdefault("chunk_index", row.chunk_index)
                metadata["score_components"] = {
                    "cosine": round(float(cosine), 4),
                    "lexical": round(float(lexical), 4),
                }
                results.append(
                    RetrievedChunk(
                        chunk_id=row.id,
                        doc_id=row.doc_id,
                        title=row.title,
                        source=row.source,
                        text=row.text,
                        score=round(float(score), 6),
                        metadata=metadata,
                    )
                )
            return results

    # -- maintenance ----------------------------------------------------
    def count_chunks(self) -> int:
        session = self._open()
        try:
            return int(session.execute(select(func.count()).select_from(KnowledgeChunk)).scalar_one())
        finally:
            session.close()

    def stats(self) -> dict[str, int]:
        session = self._open()
        try:
            chunks = int(
                session.execute(select(func.count()).select_from(KnowledgeChunk)).scalar_one()
            )
            docs = int(
                session.execute(select(func.count(func.distinct(KnowledgeChunk.doc_id)))).scalar_one()
            )
            return {"documents": docs, "chunks": chunks}
        finally:
            session.close()

    def delete_doc(self, doc_id: str) -> int:
        session = self._open()
        try:
            result = session.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id)
            )
            session.commit()
            return int(result.rowcount or 0)
        finally:
            session.close()

    def clear(self) -> int:
        """Delete every stored chunk (used by tests/maintenance)."""
        session = self._open()
        try:
            result = session.execute(delete(KnowledgeChunk))
            session.commit()
            self._corpus_signature = None
            return int(result.rowcount or 0)
        finally:
            session.close()

    @staticmethod
    def _relative_source(file_path: Path, root: Path) -> str:
        try:
            return file_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return file_path.name


# ---------------------------------------------------------------------------
# Process-wide singleton + convenience functions
# ---------------------------------------------------------------------------

_STORE: KnowledgeStore | None = None
_STORE_LOCK = threading.Lock()


def get_knowledge_store(
    session_factory: SessionFactory | None = None,
    *,
    force_reload: bool = False,
) -> KnowledgeStore:
    """Return the process-wide knowledge store, creating it on first use.

    Settings (embedding dimension / chunk size / overlap) are read from
    ``app.core.config`` when available; explicit arguments win. Passing a
    different ``session_factory`` recreates the store (used by tests).
    """
    global _STORE
    with _STORE_LOCK:
        if (
            _STORE is not None
            and not force_reload
            and (session_factory is None or _STORE._session_factory is session_factory)
        ):
            return _STORE
        dim, chunk_size, overlap = DEFAULT_DIM, DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP
        try:
            from app.core.config import get_settings

            settings = get_settings()
            dim = int(settings.embedding_dim or DEFAULT_DIM)
            chunk_size = int(settings.rag_chunk_size or DEFAULT_CHUNK_SIZE)
            overlap = int(settings.rag_chunk_overlap or DEFAULT_OVERLAP)
        except Exception:  # pragma: no cover - config import is optional here
            logger.debug("rag_settings_unavailable_using_defaults")
        _STORE = KnowledgeStore(
            session_factory=session_factory,
            dim=dim,
            chunk_size=chunk_size,
            overlap=overlap,
        )
        return _STORE


def reset_knowledge_store() -> None:
    """Drop the cached singleton (tests / configuration changes)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None


def ingest_text(
    title: str,
    source: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    return get_knowledge_store().ingest_text(title, source, text, metadata)


def ingest_directory(path: str | Path, *, pattern: str = "**/*.md") -> int:
    return get_knowledge_store().ingest_directory(path, pattern=pattern)


def search(query: str, k: int = 5, min_score: float = 0.0) -> list[RetrievedChunk]:
    return get_knowledge_store().search(query, k=k, min_score=min_score)


__all__ = [
    "DEFAULT_DIM",
    "KnowledgeStore",
    "extract_title",
    "get_knowledge_store",
    "ingest_directory",
    "ingest_text",
    "reset_knowledge_store",
    "search",
    "stable_doc_id",
]
