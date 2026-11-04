"""Knowledge base API: ingest documents and semantic search.

The retrieval implementation lives in ``app.rag`` (owned by the tools/RAG
engineer).  All access goes through :mod:`app.api.rag_bridge`, which tolerates
the module being absent/incomplete at any given moment and raises HTTP 503 for
the API when retrieval is not wired yet.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status

from app.api.rag_bridge import RAGUnavailable, get_rag_bridge
from app.schemas.run import (
    KnowledgeIngestRequest,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _bridge():
    try:
        return get_rag_bridge()
    except RAGUnavailable as exc:
        logger.warning("knowledge_unavailable", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"knowledge base unavailable: {exc}",
        ) from exc


@router.post("/ingest")
def ingest_knowledge(payload: KnowledgeIngestRequest) -> dict[str, int]:
    """Chunk and embed a document into the knowledge store."""
    bridge = _bridge()
    try:
        added = bridge.ingest_text(
            payload.title,
            payload.source,
            payload.text,
            metadata=payload.metadata or {},
        )
    except RAGUnavailable as exc:
        logger.warning("knowledge_ingest_unavailable", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"knowledge base unavailable: {exc}",
        ) from exc
    logger.info(
        "knowledge_ingested",
        extra={"title": payload.title, "source": payload.source, "chunks_added": added},
    )
    return {"chunks_added": max(0, int(added or 0))}


@router.get("/search", response_model=KnowledgeSearchResponse)
def search_knowledge(
    q: str = Query(..., min_length=1, max_length=2000),
    k: int = Query(5, ge=1, le=50),
) -> KnowledgeSearchResponse:
    """Semantic search over ingested internal documents."""
    bridge = _bridge()
    try:
        hits = bridge.search(q, k=k)
    except RAGUnavailable as exc:
        logger.warning("knowledge_search_unavailable", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"knowledge base unavailable: {exc}",
        ) from exc
    results = [
        KnowledgeSearchResult(
            title=str(hit.get("title") or ""),
            source=str(hit.get("source") or ""),
            text=str(hit.get("text") or ""),
            score=float(hit.get("score") or 0.0),
        )
        for hit in hits
    ]
    return KnowledgeSearchResponse(results=results)
