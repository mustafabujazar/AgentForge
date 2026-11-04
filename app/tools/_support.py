"""Shared helpers for the built-in AgentForge tools.

Kept private (leading underscore) so it is not confused with a registered tool
module. Contains JSON-safety conversion, path resolution against settings /
``ToolContext.extras`` (used by tests to point at temp fixtures) and integer
clamping.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from app.tools.base import ToolContext


def resolve_sales_db(ctx: ToolContext) -> Path:
    """Absolute path to the demo sales SQLite database."""
    override = ctx.extras.get("sales_db_path") if ctx.extras else None
    return Path(override) if override else Path(ctx.settings.sales_db)


def resolve_csv_dir(ctx: ToolContext) -> Path:
    """Directory containing the demo CSV mirrors."""
    override = ctx.extras.get("csv_dir") if ctx.extras else None
    if override:
        return Path(override)
    return Path(ctx.settings.sales_db).parent


def resolve_market_dir(ctx: ToolContext) -> Path:
    """Directory containing the curated (offline) market notes."""
    override = ctx.extras.get("market_dir") if ctx.extras else None
    if override:
        return Path(override)
    return Path(ctx.settings.knowledge_path) / "market"


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def json_safe(value: Any) -> Any:
    """Convert arbitrary DB/CSV values into JSON-serializable scalars."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()
    return str(value)


def slugify(value: str, *, max_length: int = 80) -> str:
    """Filesystem-safe slug (keeps words readable for artifact names)."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return (slug or "artifact")[:max_length].strip("-") or "artifact"


def sanitize_artifact_name(name: str, *, default: str = "artifact") -> str:
    """Reduce a user-supplied artifact name to a safe basename.

    Strips directory components and traversal sequences; only letters, digits and
    ``-_.`` survive. A ``.md`` suffix is enforced.
    """
    candidate = (name or "").replace("\\", "/")
    candidate = candidate.rsplit("/", 1)[-1].strip()
    candidate = re.sub(r"[^a-zA-Z0-9._-]+", "-", candidate).strip(".-")
    if not candidate:
        candidate = default
    if candidate.lower().endswith((".md", ".markdown", ".txt")):
        candidate = candidate.rsplit(".", 1)[0]
    return f"{candidate[:100] or default}.md"


__all__ = [
    "clamp_int",
    "json_safe",
    "resolve_csv_dir",
    "resolve_market_dir",
    "resolve_sales_db",
    "sanitize_artifact_name",
    "slugify",
]
