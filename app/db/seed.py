"""Idempotent demo-data bootstrap executed by the FastAPI lifespan.

Responsibilities
----------------
1. Ensure the demo operator account exists with the ``admin`` role (credentials
   come from ``DEMO_USER_EMAIL`` / ``DEMO_USER_PASSWORD`` in ``.env``).
2. Ingest ``data/knowledge/`` into the RAG store the first time the process runs
   against an empty knowledge table.

The bootstrap is deliberately defensive: a missing data directory, an unfinished
``app.rag`` module or a knowledge-store failure must never prevent the API from
starting (the endpoints that need RAG simply return 503 until it is ready).
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.base import get_session_factory
from app.db.models import KnowledgeChunk, User

logger = logging.getLogger(__name__)

DEMO_ADMIN_ROLE = "admin"

__all__ = ["seed_demo_data"]


def seed_demo_data(settings: Settings | None = None) -> dict[str, int]:
    """Create the demo user and seed the knowledge base.

    Returns:
        Counters ``{"users_created": int, "knowledge_chunks": int}``.
    """
    settings = settings or get_settings()
    stats = {"users_created": 0, "knowledge_chunks": 0}
    try:
        _seed_demo_user(settings, stats)
    except Exception:  # seed problems must never block app startup
        logger.exception("seed_demo_user_failed")
    try:
        _seed_knowledge(settings, stats)
    except Exception:  # pragma: no cover - defensive
        logger.exception("seed_knowledge_failed")
    logger.info(
        "seed_complete",
        extra={
            "users_created": stats["users_created"],
            "knowledge_chunks": stats["knowledge_chunks"],
        },
    )
    return stats


def _seed_demo_user(settings: Settings, stats: dict[str, int]) -> None:
    email = (settings.demo_user_email or "").strip().lower()
    password = settings.demo_user_password or ""
    if not email or not password:
        logger.warning("seed_demo_user_skipped", extra={"reason": "demo credentials not configured"})
        return

    session = get_session_factory()()
    try:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                full_name=(settings.demo_user_name or "Demo Operator").strip(),
                hashed_password=hash_password(password),
                role=DEMO_ADMIN_ROLE,
            )
            session.add(user)
            session.commit()
            stats["users_created"] += 1
            logger.info(
                "seed_demo_user_created",
                extra={"email": email, "role": DEMO_ADMIN_ROLE},
            )
            return
        changed = False
        if (user.role or "").strip().lower() != DEMO_ADMIN_ROLE:
            user.role = DEMO_ADMIN_ROLE
            changed = True
        if not user.full_name and settings.demo_user_name:
            user.full_name = settings.demo_user_name
            changed = True
        if changed:
            session.add(user)
            session.commit()
        logger.info("seed_demo_user_present", extra={"email": email})
    finally:
        session.close()


def _seed_knowledge(settings: Settings, stats: dict[str, int]) -> None:
    session = get_session_factory()()
    try:
        existing = int(session.scalar(select(func.count(KnowledgeChunk.id))) or 0)
    finally:
        session.close()
    if existing > 0:
        logger.info("seed_knowledge_present", extra={"chunks": existing})
        return

    knowledge_path = settings.knowledge_path
    if not knowledge_path.exists():
        logger.warning("seed_knowledge_missing", extra={"path": str(knowledge_path)})
        return

    # Imported lazily so a partially-built RAG layer cannot break startup.
    try:
        from app.api.rag_bridge import RAGUnavailable, get_rag_bridge

        bridge = get_rag_bridge()
        added = bridge.ingest_directory(knowledge_path)
    except RAGUnavailable as exc:
        logger.warning(
            "seed_knowledge_skipped",
            extra={"path": str(knowledge_path), "reason": str(exc)},
        )
        return
    except Exception as exc:  # pragma: no cover - depends on parallel work
        logger.warning(
            "seed_knowledge_failed",
            extra={"path": str(knowledge_path), "error": str(exc)},
        )
        return

    stats["knowledge_chunks"] = max(0, int(added or 0))
    logger.info(
        "seed_knowledge_ingested",
        extra={"path": str(knowledge_path), "chunks": stats["knowledge_chunks"]},
    )
