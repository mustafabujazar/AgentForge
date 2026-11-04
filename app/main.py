"""AgentForge FastAPI application: routers, lifespan bootstrap and SPA serving.

Run locally with::

    venv/Scripts/python -m uvicorn app.main:app --reload

The app is import-safe at every point in time: the database schema is created and
demo data seeded inside the lifespan, routers are included under ``/api``, and the
React build is served when ``frontend/dist`` exists (the API root/``/api`` keeps
working before the frontend has been built).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import artifacts as artifacts_api
from app.api import auth as auth_api
from app.api import knowledge as knowledge_api
from app.api import meta as meta_api
from app.api import runs as runs_api
from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.events import broker
from app.core.logging import get_logger, setup_logging
from app.db.base import get_engine, init_db
from app.db.seed import seed_demo_data

logger = get_logger("agentforge.main")

DESCRIPTION = (
    "AgentForge turns a natural-language business request into a governed "
    "multi-agent execution: plan → route → execute tools → evaluate → retry → "
    "(human approval) → synthesize deliverable, with a complete auditable trace."
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup/shutdown: schema, event-loop binding and demo-data bootstrap."""
    settings = get_settings()
    logger.info(
        "app_starting",
        extra={"environment": settings.environment, "version": settings.app_version},
    )
    init_db()
    broker.bind_loop()
    try:
        seed_demo_data(settings)
    except Exception:  # pragma: no cover - seed_demo_data is already defensive
        logger.exception("seed_failed")
    try:
        database = get_engine().dialect.name
    except Exception:  # pragma: no cover - defensive
        database = "unavailable"
    logger.info("app_ready", extra={"database": database})
    try:
        yield
    finally:
        broker.clear()
        logger.info("app_stopped")


def _register_api_root(application: FastAPI, settings: Settings) -> None:
    @application.get("/api", tags=["meta"])
    def api_root() -> dict[str, object]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "status": "ok",
            "environment": settings.environment,
            "docs": "/docs",
            "openapi": "/openapi.json",
        }

    @application.get("/api/", tags=["meta"], include_in_schema=False)
    def api_root_trailing_slash() -> dict[str, object]:
        return api_root()


def _mount_frontend(application: FastAPI, settings: Settings) -> None:
    """Serve the built SPA when present; otherwise expose a JSON root."""
    dist = settings.frontend_dist
    index = dist / "index.html"
    if not index.is_file():
        @application.get("/", include_in_schema=False)
        def root_info() -> dict[str, object]:
            return {
                "name": settings.app_name,
                "version": settings.app_version,
                "status": "ok",
                "message": (
                    "AgentForge API is running. Frontend build not found "
                    "(frontend/dist); see /api, /docs and /openapi.json."
                ),
                "docs": "/docs",
            }

        logger.info("frontend_not_built", extra={"dist": str(dist)})
        return

    assets = dist / "assets"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=str(assets)), name="assets")
    try:
        dist_root = dist.resolve()
    except OSError:  # pragma: no cover - defensive
        dist_root = dist

    @application.get("/", include_in_schema=False)
    def spa_index() -> FileResponse:
        return FileResponse(index)

    # Registered after every /api router so it can never shadow the API.
    @application.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        if full_path:
            candidate = dist / full_path
            try:
                resolved = candidate.resolve()
            except OSError:  # pragma: no cover - defensive
                resolved = candidate
            try:
                resolved.relative_to(dist_root)
            except ValueError:
                pass  # traversal attempt: fall through to index.html
            else:
                if resolved.is_file():
                    return FileResponse(resolved)
        return FileResponse(index)

    logger.info("frontend_mounted", extra={"dist": str(dist)})


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application (factory used by tests and scripts)."""
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_json)

    application = FastAPI(
        title=settings.app_name or "AgentForge",
        version=settings.app_version,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    register_exception_handlers(application)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins or ["*"]),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    application.include_router(auth_api.router, prefix="/api")
    application.include_router(runs_api.router, prefix="/api")
    application.include_router(artifacts_api.router, prefix="/api")
    application.include_router(knowledge_api.router, prefix="/api")
    application.include_router(meta_api.router, prefix="/api")

    _register_api_root(application, settings)
    _mount_frontend(application, settings)

    logger.info(
        "app_created",
        extra={
            "environment": settings.environment,
            "cors_origins": len(settings.cors_origins or []),
            "project_root": str(PROJECT_ROOT),
        },
    )
    return application


app = create_app()

__all__ = ["app", "create_app", "lifespan"]
