"""Database engine, session management and schema bootstrap.

``DATABASE_URL`` may point at SQLite (default, zero-setup local dev) or PostgreSQL
(``postgresql+psycopg://...``).  SQLite gets WAL mode, foreign keys and a busy
timeout so the API and background workflow threads can share the file safely.
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine(url: str, echo: bool = False) -> Engine:
    kwargs: dict = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        # Ensure the parent directory exists for file-backed SQLite databases.
        if ":///" in url and ":memory:" not in url:
            raw_path = url.split(":///", 1)[1]
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            if ":memory:" not in url:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    kwargs.update({"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20})
    if url.startswith("postgresql"):
        # Fail fast instead of hanging when a database is unreachable (local misconfig).
        kwargs["connect_args"] = {"connect_timeout": 5}
    return create_engine(url, **kwargs)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = _build_engine(settings.database_url, echo=settings.sqlite_echo and settings.is_sqlite)
        logger.info("database_engine_created", extra={"url": _redact(settings.database_url)})
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _session_factory


def init_db(engine: Engine | None = None) -> None:
    """Create all tables (idempotent). Production migrations: see docs/deployment.md."""
    target = engine or get_engine()
    Base.metadata.create_all(bind=target)
    logger.info("database_schema_ready", extra={"tables": len(Base.metadata.tables)})


def configure_database(url: str) -> None:
    """Reconfigure the global engine (used by tests and scripts)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = _build_engine(url)
    _session_factory = sessionmaker(
        bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a short-lived session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager for services and background work."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _redact(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


# Convenience aliases used throughout the app (`from app.db.base import SessionLocal`).
SessionLocal = get_session_factory()


__all__ = [
    "SessionLocal",
    "configure_database",
    "get_db",
    "get_engine",
    "get_session_factory",
    "init_db",
    "session_scope",
]
