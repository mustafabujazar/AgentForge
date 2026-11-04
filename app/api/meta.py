"""Meta/introspection endpoints: agents catalog, tools registry, health, metrics.

These endpoints are read-only and tolerant of parallel work elsewhere in the
codebase: the orchestrator agent registry and tool plugins are imported lazily,
and a static-but-accurate catalog is served when they are not available yet.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, is_admin
from app.core.config import get_settings
from app.db.models import EvaluationMetric, Run, TaskRecord, User
from app.llm.registry import provider_info
from app.schemas.run import AgentInfo, RunMetricsResponse, ToolInfo
from app.schemas.workflow import RunStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["meta"])


class AgentsResponse(BaseModel):
    agents: list[AgentInfo] = Field(default_factory=list)


class ToolsResponse(BaseModel):
    tools: list[ToolInfo] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    version: str
    llm_provider: str
    llm_model: str
    database: str
    environment: str


# ---------------------------------------------------------------------------
# Agents catalog
# ---------------------------------------------------------------------------
_STATIC_AGENTS: tuple[dict[str, Any], ...] = (
    {
        "name": "research",
        "role": "research",
        "description": (
            "Gathers evidence from the internal knowledge base and external sources, "
            "then condenses it into grounded findings with citations."
        ),
        "capabilities": [
            "knowledge retrieval",
            "web research",
            "source triangulation",
            "context summarization",
        ],
        "tools": ["knowledge_search", "web_search"],
    },
    {
        "name": "analyst",
        "role": "analyst",
        "description": (
            "Runs quantitative analysis over the sales dataset (rankings, trends, "
            "drivers, segment breakdowns) and produces structured metrics."
        ),
        "capabilities": [
            "SQL analysis",
            "CSV aggregation",
            "ranking and trend detection",
            "metric computation",
        ],
        "tools": ["sales_sql", "csv_query"],
    },
    {
        "name": "execution",
        "role": "execution",
        "description": (
            "Performs action-oriented operational tasks; sensitive actions pause the "
            "run for explicit human approval before they execute."
        ),
        "capabilities": [
            "approved actions",
            "deliverable generation",
            "record updates",
            "controlled side effects",
        ],
        "tools": ["report_writer"],
    },
    {
        "name": "reporting",
        "role": "reporting",
        "description": (
            "Synthesizes task results, evaluations and sources into a cited, "
            "decision-ready Markdown business report with recommendations."
        ),
        "capabilities": [
            "report synthesis",
            "executive summaries",
            "recommendations",
            "source citation",
        ],
        "tools": ["report_writer", "knowledge_search"],
    },
)


def _read_value(source: Any, *keys: str) -> Any:
    """Best-effort attribute/key read used for duck-typed agent registries."""
    if source is None:
        return None
    if isinstance(source, dict):
        for key in keys:
            if key in source and source[key] not in (None, ""):
                return source[key]
        return None
    for key in keys:
        try:
            value = getattr(source, key, None)
        except Exception:  # pragma: no cover - properties may raise
            value = None
        if value not in (None, ""):
            return value
    return None


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(key) for key in value.keys()]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _agent_info(item: Any) -> AgentInfo | None:
    candidates: list[Any] = [item]
    for attr in ("spec", "profile", "config", "metadata", "info"):
        try:
            nested = getattr(item, attr, None)
        except Exception:  # pragma: no cover - defensive
            nested = None
        if nested is not None:
            candidates.append(nested)
    for candidate in candidates:
        name = _read_value(candidate, "name", "agent_name")
        if not name:
            continue
        role_raw = _read_value(candidate, "role", "agent_role") or name
        role = getattr(role_raw, "value", role_raw)
        description = _read_value(candidate, "description", "summary", "purpose") or ""
        capabilities = _as_string_list(
            _read_value(candidate, "capabilities", "skills", "supported_tasks")
        )
        tools = _as_string_list(
            _read_value(candidate, "tools", "tool_names", "allowed_tools", "tool_hints")
        )
        if not capabilities and candidate is not item:
            capabilities = _as_string_list(_read_value(item, "capabilities", "skills"))
        if not tools and candidate is not item:
            tools = _as_string_list(
                _read_value(item, "tools", "tool_names", "allowed_tools")
            )
        return AgentInfo(
            name=str(name),
            role=str(role),
            description=str(description),
            capabilities=capabilities,
            tools=tools,
        )
    return None


def _dynamic_agents() -> list[AgentInfo] | None:
    """Try to surface the orchestrator's agent registry when it exists."""
    try:
        import app.agents as agents_pkg
    except Exception:
        return None

    container: Any = None
    for attr in ("AGENT_REGISTRY", "AGENTS", "AGENT_CLASSES", "registry", "get_agents"):
        try:
            candidate = getattr(agents_pkg, attr, None)
        except Exception:  # pragma: no cover - defensive
            candidate = None
        if candidate is not None:
            container = candidate
            break
    if container is None:
        return None
    if callable(container) and not isinstance(container, type):
        try:
            container = container()
        except Exception:
            return None
    if isinstance(container, dict):
        items = list(container.values())
    elif isinstance(container, (list, tuple, set)):
        items = list(container)
    else:
        return None
    agents = [info for item in items if (info := _agent_info(item)) is not None]
    return agents or None


def agent_catalog() -> list[AgentInfo]:
    """Static catalog enriched by the live orchestrator registry when present."""
    catalog: dict[str, AgentInfo] = {
        entry["name"]: AgentInfo(**entry) for entry in _STATIC_AGENTS
    }
    for agent in _dynamic_agents() or []:
        catalog[agent.name] = agent
    return list(catalog.values())


@router.get("/agents", response_model=AgentsResponse)
def list_agents() -> AgentsResponse:
    """The specialized agent roster available to the orchestrator."""
    return AgentsResponse(agents=agent_catalog())


# ---------------------------------------------------------------------------
# Tools registry
# ---------------------------------------------------------------------------
@router.get("/tools", response_model=ToolsResponse)
def list_tools() -> ToolsResponse:
    """Every tool exposed to agents (names, schemas, scopes, sensitivity)."""
    try:
        from app.tools.registry import get_registry

        specs = get_registry().to_dicts()
    except Exception as exc:  # pragma: no cover - registry is defensive already
        logger.warning("tool_registry_unavailable", extra={"error": str(exc)})
        specs = []
    tools: list[ToolInfo] = []
    for spec in specs:
        try:
            tools.append(ToolInfo.model_validate(spec))
        except Exception:  # pragma: no cover - malformed third-party tool spec
            logger.warning("tool_spec_skipped", extra={"tool": spec.get("name") if isinstance(spec, dict) else None})
    return ToolsResponse(tools=tools)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness/readiness probe with provider and database facts."""
    settings = get_settings()
    try:
        info = provider_info(settings)
    except Exception:  # pragma: no cover - provider_info is non-throwing
        info = {}
    try:
        from app.db.base import get_engine

        database = get_engine().dialect.name
    except Exception:  # pragma: no cover - DB engine misconfiguration
        database = "unavailable"
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        llm_provider=str(info.get("provider") or settings.llm_provider),
        llm_model=str(info.get("model") or settings.llm_model),
        database=database,
        environment=settings.environment,
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def _duration_seconds(run: Run) -> float | None:
    end = run.completed_at
    start = run.started_at or run.created_at
    if end is None or start is None:
        return None
    try:
        seconds = (end - start).total_seconds()
    except TypeError:
        # Mixed naive/aware datetimes can appear across SQLite/Postgres drivers.
        try:
            seconds = (
                end.replace(tzinfo=None) - start.replace(tzinfo=None)
            ).total_seconds()
        except Exception:  # pragma: no cover - defensive
            return None
    except Exception:  # pragma: no cover - defensive
        return None
    return max(float(seconds), 0.0)


@router.get("/metrics/runs", response_model=RunMetricsResponse)
def run_metrics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunMetricsResponse:
    """Aggregate workflow metrics across the runs visible to the caller."""
    stmt = select(Run)
    if not is_admin(current_user):
        stmt = stmt.where(Run.user_id == current_user.id)
    runs = db.scalars(stmt).all()

    total_runs = len(runs)
    by_status: dict[str, int] = {status.value: 0 for status in RunStatus}
    for run in runs:
        key = (run.status or "").strip().lower()
        by_status[key] = by_status.get(key, 0) + 1
    completed = by_status.get(RunStatus.COMPLETED.value, 0)
    failed = by_status.get(RunStatus.FAILED.value, 0)
    success_rate = round(completed / total_runs, 4) if total_runs else 0.0

    durations = [
        duration for run in runs if (duration := _duration_seconds(run)) is not None
    ]
    avg_duration = round(sum(durations) / len(durations), 2) if durations else None

    total_tokens = 0
    for run in runs:
        usage = run.token_usage_json or {}
        try:
            total_tokens += int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError, AttributeError):
            continue

    total_retries = 0
    avg_eval_score: float | None = None
    run_ids = [run.id for run in runs]
    if run_ids:
        attempts = db.scalars(
            select(TaskRecord.attempts).where(TaskRecord.run_id.in_(run_ids))
        ).all()
        total_retries = sum(max(int(attempt or 0) - 1, 0) for attempt in attempts)
        scores = db.scalars(
            select(EvaluationMetric.score).where(EvaluationMetric.run_id.in_(run_ids))
        ).all()
        if scores:
            avg_eval_score = round(sum(float(score) for score in scores) / len(scores), 4)

    return RunMetricsResponse(
        total_runs=total_runs,
        completed=completed,
        failed=failed,
        success_rate=success_rate,
        avg_duration_seconds=avg_duration,
        total_tokens=total_tokens,
        total_retries=total_retries,
        avg_eval_score=avg_eval_score,
        by_status=by_status,
    )
