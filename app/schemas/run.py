"""API response/request schemas for runs, tasks, events, approvals, artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import (
    AgentRole,
    Evaluation,
    Plan,
    RunStatus,
    TaskStatus,
    TokenUsage,
)


class RunCreate(BaseModel):
    objective: str = Field(min_length=12, max_length=4000)
    dataset: str | None = None
    auto_approve: bool | None = None


class PendingApprovalRead(BaseModel):
    id: str
    task_id: str
    title: str
    requested_action: str
    reason: str = ""


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    objective: str
    status: RunStatus
    plan: Plan | None = None
    final_report: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    pending_approval: PendingApprovalRead | None = None

    @classmethod
    def from_run(cls, run: Any, pending_approval: PendingApprovalRead | None = None) -> "RunRead":
        plan = None
        if getattr(run, "plan_json", None):
            try:
                plan = Plan.model_validate(run.plan_json)
            except Exception:
                plan = None
        usage = TokenUsage.model_validate(getattr(run, "token_usage_json", None) or {})
        return cls(
            id=run.id,
            objective=run.objective,
            status=RunStatus(run.status),
            plan=plan,
            final_report=run.final_report,
            error=run.error,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            token_usage=usage,
            pending_approval=pending_approval,
        )


class TaskRead(BaseModel):
    id: str
    task_id: str
    title: str
    agent: AgentRole
    status: TaskStatus
    attempts: int = 0
    result: dict[str, Any] | None = None
    evaluation: Evaluation | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_record(cls, record: Any) -> "TaskRead":
        evaluation = None
        if getattr(record, "evaluation_json", None):
            try:
                evaluation = Evaluation.model_validate(record.evaluation_json)
            except Exception:
                evaluation = None
        return cls(
            id=record.id,
            task_id=record.task_id,
            title=record.title,
            agent=AgentRole(record.agent),
            status=TaskStatus(record.status),
            attempts=record.attempts,
            result=record.result_json,
            evaluation=evaluation,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class RunEventRead(BaseModel):
    seq: int
    run_id: str
    type: str
    ts: datetime | None = None
    agent: str | None = None
    task_id: str | None = None
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = None

    @classmethod
    def from_event(cls, event: Any) -> "RunEventRead":
        return cls(
            seq=event.seq,
            run_id=event.run_id,
            type=event.type,
            ts=event.created_at,
            agent=event.agent,
            task_id=event.task_id,
            message=event.message or "",
            data=event.data_json or {},
            duration_ms=event.duration_ms,
        )


class ApprovalRead(BaseModel):
    id: str
    run_id: str
    task_id: str
    title: str
    requested_action: str
    reason: str = ""
    risk_level: str = "medium"
    status: str
    note: str | None = None
    created_at: datetime | None = None
    decided_at: datetime | None = None


class ApprovalDecision(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str | None = Field(default=None, max_length=2000)


class ArtifactRead(BaseModel):
    id: str
    run_id: str
    name: str
    kind: str
    path: str
    description: str = ""
    created_at: datetime | None = None


class RunListResponse(BaseModel):
    items: list[RunRead]
    total: int


class ReportResponse(BaseModel):
    run_id: str
    report: str | None
    artifacts: list[ArtifactRead] = Field(default_factory=list)


class RunMetricsResponse(BaseModel):
    total_runs: int
    completed: int
    failed: int
    success_rate: float
    avg_duration_seconds: float | None = None
    total_tokens: int
    total_retries: int
    avg_eval_score: float | None = None
    by_status: dict[str, int] = Field(default_factory=dict)


class AgentInfo(BaseModel):
    name: str
    role: str
    description: str
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class ToolInfo(BaseModel):
    name: str
    description: str
    category: str = "general"
    sensitive: bool = False
    scopes: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)


class KnowledgeIngestRequest(BaseModel):
    title: str
    source: str = "manual"
    text: str = Field(min_length=20)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchResult(BaseModel):
    title: str
    source: str
    text: str
    score: float


class KnowledgeSearchResponse(BaseModel):
    results: list[KnowledgeSearchResult]


class HealthResponse(BaseModel):
    status: str
    version: str
    llm_provider: str
    llm_model: str
    database: str
    environment: str
