"""Global tool registry with lazy discovery, schema validation and scope checks."""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any

from app.core.errors import ToolExecutionError
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

# Modules that are imported lazily on first registry access.  Each module may
# expose a ``TOOL`` instance, a ``TOOLS`` list, or a ``register_tools(registry)``.
DEFAULT_TOOL_MODULES: tuple[str, ...] = (
    "app.tools.sales_sql",
    "app.tools.csv_query",
    "app.tools.knowledge_search",
    "app.tools.web_search",
    "app.tools.report_writer",
)

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._lock = threading.Lock()
        self._defaults_loaded = False

    # ------------------------------------------------------------------
    def register(self, tool: BaseTool) -> None:
        if not isinstance(tool, BaseTool):
            raise TypeError(f"tool must extend BaseTool, got {type(tool)!r}")
        name = tool.spec.name
        if not name:
            raise ValueError("tool spec is missing a name")
        with self._lock:
            if name in self._tools:
                logger.debug("tool_registered_duplicate", extra={"tool": name})
            self._tools[name] = tool
        logger.info("tool_registered", extra={"tool": name, "sensitive": tool.spec.sensitive})

    def get(self, name: str) -> BaseTool | None:
        self.ensure_defaults()
        return self._tools.get(name)

    def require(self, name: str) -> BaseTool:
        tool = self.get(name)
        if tool is None:
            raise ToolExecutionError(f"unknown tool: {name}")
        return tool

    def list_tools(self) -> list[BaseTool]:
        self.ensure_defaults()
        return list(self._tools.values())

    def list_specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.list_tools()]

    def tool_names(self) -> list[str]:
        return sorted(tool.spec.name for tool in self.list_tools())

    def to_dicts(self) -> list[dict[str, Any]]:
        return [tool.to_dict() for tool in self.list_tools()]

    # ------------------------------------------------------------------
    def ensure_defaults(self) -> None:
        if self._defaults_loaded:
            return
        self._defaults_loaded = True
        for module_name in DEFAULT_TOOL_MODULES:
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # missing tool modules must not break the app
                logger.warning(
                    "tool_module_unavailable",
                    extra={"tool_module": module_name, "error": str(exc)},
                )
                continue
            registered = False
            if hasattr(module, "register_tools"):
                try:
                    module.register_tools(self)
                    registered = True
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "tool_register_failed",
                        extra={"tool_module": module_name, "error": str(exc)},
                    )
            if hasattr(module, "TOOL"):
                self.register(module.TOOL)
                registered = True
            for tool in getattr(module, "TOOLS", []) or []:
                self.register(tool)
                registered = True
            if not registered:
                logger.warning("tool_module_empty", extra={"tool_module": module_name})

    # ------------------------------------------------------------------
    def execute(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        ctx: ToolContext | None = None,
        *,
        allowed_scopes: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.failure(f"unknown tool: {name}")
        if ctx is None:
            return ToolResult.failure("tool context is required")
        args = args or {}

        if allowed_scopes is not None:
            missing = set(tool.spec.scopes) - set(allowed_scopes)
            if missing:
                return ToolResult.failure(
                    f"tool '{name}' requires scopes not granted to agent "
                    f"'{ctx.agent}': {sorted(missing)}"
                )

        if tool.spec.sensitive and not ctx.extras.get("approval_granted"):
            return ToolResult.failure(
                f"tool '{name}' is sensitive and requires human approval before execution"
            )

        validation_error = _validate_args(tool.spec, args)
        if validation_error:
            return ToolResult.failure(validation_error)

        started = time.perf_counter()
        try:
            result = tool.run(args, ctx)
            if not isinstance(result, ToolResult):
                result = ToolResult.success(result)
        except ToolExecutionError as exc:
            result = ToolResult.failure(str(exc))
        except Exception as exc:  # never let a tool crash the workflow
            logger.exception("tool_execution_failed", extra={"tool": name})
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")
        result.duration_ms = result.duration_ms or int((time.perf_counter() - started) * 1000)
        logger.info(
            "tool_executed",
            extra={
                "tool": name,
                "agent": ctx.agent,
                "run_id": ctx.run_id,
                "ok": result.ok,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    def clear(self) -> None:
        with self._lock:
            self._tools.clear()
        self._defaults_loaded = False


def _validate_args(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    schema = spec.input_schema or {}
    if not isinstance(args, dict):
        return "tool arguments must be a JSON object"
    required = schema.get("required") or []
    for key in required:
        if key not in args:
            return f"missing required argument: {key}"
    properties = schema.get("properties") or {}
    for key, value in args.items():
        field_schema = properties.get(key)
        if not isinstance(field_schema, dict):
            continue
        expected_type = field_schema.get("type")
        if expected_type not in _TYPE_MAP:
            continue
        if expected_type in {"number", "integer"} and isinstance(value, bool):
            return f"argument '{key}' must be a {expected_type}"
        if not isinstance(value, _TYPE_MAP[expected_type]):
            return f"argument '{key}' must be a {expected_type}"
    return None


_REGISTRY = ToolRegistry()


def get_registry() -> ToolRegistry:
    _REGISTRY.ensure_defaults()
    return _REGISTRY


def reset_registry() -> None:
    _REGISTRY.clear()


__all__ = [
    "DEFAULT_TOOL_MODULES",
    "ToolRegistry",
    "get_registry",
    "reset_registry",
]
