"""Tool contracts shared by agents, the registry and the API.

A tool is a small, typed, side-effect-explicit capability (SQL query, knowledge
retrieval, report writing...).  Tools are registered in a singleton registry and
invoked by agents through ``ToolRegistry.execute`` with scope + approval checks.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.core.config import Settings, get_settings

SessionFactory = Callable[[], Any]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    scopes: tuple[str, ...] = ()
    sensitive: bool = False
    category: str = "general"
    timeout_seconds: int = 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "scopes": list(self.scopes),
            "sensitive": self.sensitive,
            "category": self.category,
        }


@dataclass(slots=True)
class ToolContext:
    run_id: str
    agent: str
    settings: Settings = field(default_factory=get_settings)
    artifacts_dir: Path | None = None
    db_session_factory: SessionFactory | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved_artifacts_dir(self) -> Path:
        path = self.artifacts_dir or self.settings.artifacts_path
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: Any = None
    error: str | None = None
    duration_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, output: Any, *, duration_ms: int = 0, meta: dict[str, Any] | None = None) -> "ToolResult":
        return cls(ok=True, output=output, duration_ms=duration_ms, meta=meta or {})

    @classmethod
    def failure(cls, error: str, *, duration_ms: int = 0, meta: dict[str, Any] | None = None) -> "ToolResult":
        return cls(ok=False, output=None, error=error, duration_ms=duration_ms, meta=meta or {})

    def preview(self, limit: int = 1200) -> str:
        import json

        try:
            text = self.output if isinstance(self.output, str) else json.dumps(self.output, default=str)
        except (TypeError, ValueError):
            text = repr(self.output)
        return text[:limit]


class BaseTool(ABC):
    spec: ToolSpec

    @abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Execute the tool. Implementations must be side-effect explicit."""

    def to_dict(self) -> dict[str, Any]:
        return self.spec.to_dict()


__all__ = ["BaseTool", "SessionFactory", "ToolContext", "ToolResult", "ToolSpec"]
