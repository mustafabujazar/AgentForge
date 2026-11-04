"""Execution agent: turns analysis into a remediation / action plan.

Sensitive actions are gated by human approval at the workflow level: the executor
only grants ``approval_granted=True`` after an approved HITL gate (or auto-approve).
Without approval the agent plans but never claims to have executed anything.
"""

from __future__ import annotations

from app.agents.base import AgentContext, BaseAgent
from app.schemas.workflow import AgentResult, AgentRole, TaskSpec

# Tools that represent real-world actions rather than reporting.  Only exposed to
# the execution agent after approval, and only if the tools-rag team registers them.
ACTION_TOOL_CANDIDATES = (
    "action_dispatch",
    "execute_action",
    "send_email",
    "create_campaign",
    "apply_discount",
    "update_price",
    "notify_owner",
)


class ExecutionAgent(BaseAgent):
    role = AgentRole.EXECUTION
    name = "execution"
    description = (
        "Drafts a prioritized remediation action plan (owner, action, expected impact, "
        "risk), marking sensitive/action-taking steps for human approval."
    )
    capabilities = (
        "remediation planning",
        "action prioritization",
        "sensitive-action governance",
        "risk assessment",
    )
    allowed_tools = ("report_writer",)
    scopes = ("write:artifacts",)
    max_tool_steps = 2
    temperature = 0.2

    def resolve_tool_names(self, ctx: AgentContext) -> tuple[str, ...]:
        names: list[str] = ["report_writer"]
        try:
            specs = ctx.registry.list_specs()
        except Exception:  # pragma: no cover - registry discovery defensive
            specs = []
        for spec in specs:
            if spec.name in ACTION_TOOL_CANDIDATES and spec.name not in names:
                names.append(spec.name)
            elif spec.sensitive and spec.name not in names:
                names.append(spec.name)
        return tuple(names)

    def role_instructions(self) -> str:
        return (
            "Produce a concrete remediation/action plan, not more analysis. Each action must "
            "have: action, owner (team/role), expected impact (quantified if possible), effort, "
            "and risk. Explicitly mark any real-world/sensitive step as SENSITIVE — e.g. pricing "
            "changes, discounts, campaign launches, refunds, customer emails. If approval is "
            "granted AND an action tool is available, you may execute the approved action and "
            "report the tool result; otherwise draft the plan and state clearly that execution "
            "requires human approval. Put the full plan markdown in 'artifact_markdown' and call "
            "report_writer once to persist it when available."
        )

    def postprocess(self, result: AgentResult, ctx: AgentContext, task: TaskSpec) -> AgentResult:
        markdown = str(result.metrics.get("artifact_markdown") or "")
        if markdown and not any(artifact.name.endswith(".md") for artifact in result.artifacts):
            artifact = self.write_artifact(
                ctx,
                task,
                markdown,
                suffix="action-plan",
                description="Remediation action plan with owners, impact and risk.",
            )
            if artifact is not None:
                result.artifacts.append(artifact)
        result.metrics["sensitive"] = bool(task.sensitive)
        result.metrics["approval_granted"] = bool(ctx.approval_granted)
        result.metrics["action_plan_chars"] = len(markdown)
        return result


__all__ = ["ACTION_TOOL_CANDIDATES", "ExecutionAgent"]
