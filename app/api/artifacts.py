"""Artifact listing and secure download endpoints.

Artifacts are Markdown deliverables (the synthesized report, generated exports)
written by the workflow.  Downloads are authenticated and ownership-checked; if
the file on disk is missing the persisted ``Artifact.content`` column is used as
a fallback so old runs remain downloadable after cleanup.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, is_admin
from app.core.config import PROJECT_ROOT, get_settings
from app.core.errors import NotFoundError
from app.db.models import Artifact, Run, User
from app.schemas.run import ArtifactRead

logger = logging.getLogger(__name__)

router = APIRouter(tags=["artifacts"])

_MEDIA_TYPES = {
    "markdown": "text/markdown",
    "md": "text/markdown",
    "text": "text/plain",
    "txt": "text/plain",
    "json": "application/json",
    "csv": "text/csv",
    "html": "text/html",
}


class ArtifactListResponse(BaseModel):
    items: list[ArtifactRead] = Field(default_factory=list)


def _artifact_read(artifact: Artifact) -> ArtifactRead:
    return ArtifactRead(
        id=artifact.id,
        run_id=artifact.run_id,
        name=artifact.name,
        kind=artifact.kind or "markdown",
        path=artifact.path or "",
        description=artifact.description or "",
        created_at=artifact.created_at,
    )


def _media_type(kind: str | None) -> str:
    return _MEDIA_TYPES.get((kind or "").strip().lower(), "text/markdown")


def _safe_filename(name: str | None) -> str:
    base = (name or "artifact.md").strip().replace("\\", "/").split("/")[-1]
    return base or "artifact.md"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_artifact_path(raw_path: str | None) -> Path | None:
    """Resolve the stored path, refusing files outside the workspace/artifacts."""
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    settings = get_settings()
    allowed_roots: list[Path] = []
    for root in (PROJECT_ROOT, settings.artifacts_path):
        try:
            allowed_roots.append(root.resolve())
        except OSError:  # pragma: no cover - defensive
            continue
    if allowed_roots and not any(_is_within(resolved, root) for root in allowed_roots):
        logger.warning("artifact_path_outside_workspace", extra={"path": str(resolved)})
        return None
    return resolved


def _owned_run(db: Session, run_id: str, user: User) -> Run:
    run = db.get(Run, run_id)
    if run is None or (not is_admin(user) and run.user_id != user.id):
        raise NotFoundError("run not found")
    return run


@router.get("/runs/{run_id}/artifacts", response_model=ArtifactListResponse)
def list_run_artifacts(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArtifactListResponse:
    """All artifacts produced by a run (newest last)."""
    run = _owned_run(db, run_id, current_user)
    artifacts = db.scalars(
        select(Artifact).where(Artifact.run_id == run.id).order_by(Artifact.created_at)
    ).all()
    return ArtifactListResponse(items=[_artifact_read(artifact) for artifact in artifacts])


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download an artifact as Markdown, falling back to the DB-stored content."""
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise NotFoundError("artifact not found")
    _owned_run(db, artifact.run_id, current_user)

    filename = _safe_filename(artifact.name)
    media_type = _media_type(artifact.kind)

    resolved = _resolve_artifact_path(artifact.path)
    if resolved is not None:
        return FileResponse(path=resolved, media_type=media_type, filename=filename)

    if artifact.content is not None:
        return Response(
            content=artifact.content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    raise NotFoundError("artifact has no downloadable content")
