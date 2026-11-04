"""AgentForge retrieval layer: markdown chunking + deterministic local embeddings.

Usage::

    from app.rag import get_knowledge_store, ingest_directory, search

    store = get_knowledge_store()
    store.ingest_directory("data/knowledge")
    hits = store.search("why did Solstice returns spike", k=5)

Design notes live in ``docs/rag.md``. The default embedder is a deterministic
hashed n-gram TF-IDF model (no network, no extra dependencies); vectors persist in
the ``knowledge_chunks`` table so retrieval survives restarts and works on both
SQLite and PostgreSQL.
"""

from __future__ import annotations

from app.rag.chunking import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, chunk_markdown, split_sections
from app.rag.embeddings import Embedder, LocalHashEmbedder, cosine_similarity, tokenize
from app.rag.store import (
    DEFAULT_DIM,
    KnowledgeStore,
    extract_title,
    get_knowledge_store,
    ingest_directory,
    ingest_text,
    reset_knowledge_store,
    search,
    stable_doc_id,
)
from app.rag.types import Chunk, RetrievedChunk

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_DIM",
    "DEFAULT_OVERLAP",
    "Chunk",
    "Embedder",
    "KnowledgeStore",
    "LocalHashEmbedder",
    "RetrievedChunk",
    "chunk_markdown",
    "cosine_similarity",
    "extract_title",
    "get_knowledge_store",
    "ingest_directory",
    "ingest_text",
    "reset_knowledge_store",
    "search",
    "split_sections",
    "stable_doc_id",
    "tokenize",
]
