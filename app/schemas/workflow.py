"""Workflow domain schemas shared by the orchestrator, agents, LLM layer and API."""

from __future__ import annotations

from enum import Enum
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentRole(str, Enum):
    PLANNER = "planner"
    RESEARCH = "research"
    ANALYST = "analyst"
    EXECUTION = "execution"
    REPORTING = "reporting"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    AWAITING_APPROVAL = "awaiting_approval"
    SKIPPED = "skipped"


_AGENT_ALIASES = {
    "planner": AgentRole.PLANNER,
    "orchestrator": AgentRole.PLANNER,
    "research": AgentRole.RESEARCH,
    "researcher": AgentRole.RESEARCH,
    "research_agent": AgentRole.RESEARCH,
    "analyst": AgentRole.ANALYST,
    "data_analyst": AgentRole.ANALYST,
    "analytics": AgentRole.ANALYST,
    "data": AgentRole.ANALYST,
    "execution": AgentRole.EXECUTION,
    "executor": AgentRole.EXECUTION,
    "operations": AgentRole.EXECUTION,
    "reporting": AgentRole.REPORTING,
    "reporter": AgentRole.REPORTING,
    "report": AgentRole.REPORTING,
    "writer": AgentRole.REPORTING,
}


class TaskSpec(BaseModel):
    """One unit of work assigned to one specialized agent."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="Stable task id such as 'task-1'")
    title: str
    description: str
    agent: AgentRole
    depends_on: list[str] = Field(default_factory=list)
    tool_hints: list[str] = Field(default_factory=list)
    sensitive: bool = False
    expected_output: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    priority: int = Field(default=3, ge=1, le=5)

    @field_validator("agent", mode="before")
    @classmethod
    def _normalize_agent(cls, value: Any) -> Any:
        if isinstance(value, AgentRole):
            return value
        if isinstance(value, str):
            key = value.strip().lower().replace("-", "_").replace(" ", "_")
            if key in _AGENT_ALIASES:
                return _AGENT_ALIASES[key]
            for role in AgentRole:
                if role.value == key:
                    return role
        return value

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "task"
        return raw.replace(" ", "-").lower()


class Plan(BaseModel):
    """Structured execution plan produced by the planner."""

    model_config = ConfigDict(extra="ignore")

    objective: str
    summary: str = ""
    tasks: list[TaskSpec] = Field(default_factory=list, min_length=1, max_length=8)
    success_criteria: list[str] = Field(default_factory=list)
    requires_approval: bool = False

    @field_validator("tasks")
    @classmethod
    def _dedupe_task_ids(cls, tasks: list[TaskSpec]) -> list[TaskSpec]:
        seen: set[str] = set()
        for index, task in enumerate(tasks, start=1):
            if task.id in seen or not task.id:
                task.id = f"task-{index}"
            seen.add(task.id)
        return tasks

    def task_map(self) -> dict[str, TaskSpec]:
        return {task.id: task for task in self.tasks}


class Finding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str
    evidence: str = ""
    source: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    kind: str = "markdown"
    path: str = ""
    description: str = ""


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    output_preview: str = ""
    duration_ms: int = 0
    error: str | None = None


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    agent: AgentRole
    status: TaskStatus = TaskStatus.COMPLETED
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    metrics: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    passed: bool = False
    criteria_met: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    feedback: str = ""
    should_retry: bool = False
    evaluator: str = "llm+rules"


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    run_id: str
    task_id: str
    title: str
    requested_action: str
    reason: str = ""
    risk_level: str = "medium"


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    model: str = ""
    provider: str = ""

    def add(self, other: "TokenUsage") -> "TokenUsage":
        model = self.model or other.model
        provider = self.provider or other.provider
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            calls=self.calls + other.calls,
            model=model,
            provider=provider,
        )


class WorkflowState(TypedDict, total=False):
    """Shared state threaded through the LangGraph workflow.

    Kept JSON-serializable: LangGraph checkpoints it and the runtime snapshots it
    into ``runs.state_json`` after every node for auditability/resume.
    """

    run_id: str
    objective: str
    auto_approve: bool
    plan: dict[str, Any]
    task_order: list[str]
    current_index: int
    current_task_id: str | None
    results: dict[str, dict[str, Any]]
    evaluations: dict[str, dict[str, Any]]
    attempts: dict[str, int]
    feedback: dict[str, str]
    approvals: dict[str, str]
    pending_approval_id: str | None
    awaiting_task_id: str | None
    final_report: str | None
    status: str
    error: str | None
    token_usage: dict[str, Any]
    started_at: str
    completed_at: str | None
    context: dict[str, Any]
