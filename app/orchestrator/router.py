"""Plan validation, agent capability routing, dependency ordering and sensitivity.

The planner LLM is not trusted: this module repairs agent assignments (unknown or
'missing' agents → keyword-based role), filters tool hints to registered tools,
topologically orders tasks by ``depends_on``, guarantees a reporting task, and
enforces the sensitive-task policy:

    ``sensitive=True`` is only allowed on EXECUTION tasks, and only when the
    objective actually asks the system to *act* (launch/execute/send/refund/...).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from app.schemas.workflow import AgentRole, Plan, TaskSpec

logger = logging.getLogger(__name__)

# Verbs that mean "take a real-world action" rather than just analyse/report.
_ACTION_RE = re.compile(
    r"\b("
    r"launch|execute|send|email|e-mail|refund|update|create|add|apply|deploy|"
    r"publish|schedule|notify|implement|roll\s*out|rollout|adjust|change|"
    r"discount|campaign|purchase|order|enable|disable|cancel|issue|approve|"
    r"escalate|contact|follow[-\s]?up|act\s+on|take\s+action"
    r")\b",
    re.IGNORECASE,
)

_ROLE_KEYWORDS: dict[AgentRole, tuple[str, ...]] = {
    AgentRole.RESEARCH: (
        "research", "investigat", "context", "background", "root cause", "memo",
        "knowledge", "market", "competitor", "qualitative",
    ),
    AgentRole.ANALYST: (
        "analyz", "analys", "data", "sql", "quant", "metric", "trend", "revenue",
        "kpi", "calculate", "compute", "forecast", "diagnos", "performance",
    ),
    AgentRole.EXECUTION: (
        "action", "remediat", "plan of action", "implement", "launch", "campaign",
        "execute", "mitigat", "recommend", "operations", "fix", "discount",
    ),
    AgentRole.REPORTING: (
        "report", "write", "summar", "deliver", "memo", "executive", "present",
        "compose", "document",
    ),
}

_ALIASES: dict[str, AgentRole] = {
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
    "planner": AgentRole.ANALYST,  # planner is not an executable task agent
    "orchestrator": AgentRole.ANALYST,
}

# The workflow is deliberately a single pass through four specialist agents.
# Duplicated same-role tasks are collapsed: one comprehensive analyst pass is much
# cheaper (and less error-prone) than splitting "identify" and "diagnose".
CANONICAL_ORDER: tuple[AgentRole, ...] = (
    AgentRole.RESEARCH,
    AgentRole.ANALYST,
    AgentRole.EXECUTION,
    AgentRole.REPORTING,
)


def canonical_tasks(objective: str, *, action_requested: bool) -> dict[AgentRole, TaskSpec]:
    """Canonical task definitions, one per specialist role (stable prompts/IDs)."""
    return {
        AgentRole.RESEARCH: TaskSpec(
            id="task-1",
            title="Investigate root causes and internal context",
            description=(
                "Search the internal knowledge corpus (and market notes) for WHY the business "
                "results look the way they do: quality/returns, pricing/discount policy, "
                "marketing effectiveness, support escalation. Use knowledge_search 2-3 times "
                "with business-phrased queries and cite only retrieved passages."
            ),
            agent=AgentRole.RESEARCH,
            depends_on=[],
            tool_hints=["knowledge_search", "web_search"],
            sensitive=False,
            expected_output="3-5 evidence-backed findings citing internal documents.",
            success_criteria=[
                "At least 3 findings with evidence and a named source",
                "Covers quality, pricing and marketing context where relevant",
            ],
            priority=2,
        ),
        AgentRole.ANALYST: TaskSpec(
            id="task-2",
            title="Quantify performance and root causes in one analysis",
            description=(
                "Run SQL against the demo sales database to quantify the trailing 12 months "
                "ending MAX(sale_date): revenue, units, gross margin, return rate, average "
                "discount, marketing spend and support tickets PER PRODUCT (join products and "
                "return the product name). Identify the worst performers, compare with the "
                "prior period, and explain the quantitative drivers in one pass."
            ),
            agent=AgentRole.ANALYST,
            depends_on=["task-1"],
            tool_hints=["sales_sql", "knowledge_search"],
            sensitive=False,
            expected_output="Quantified product ranking with metrics and period deltas.",
            success_criteria=[
                "Trailing-12m revenue by named product computed with SQL",
                "Period deltas, return/discount/marketing/ticket metrics computed",
                "Worst-performing products identified by name with concrete numbers",
            ],
            priority=1,
        ),
        AgentRole.EXECUTION: TaskSpec(
            id="task-3",
            title="Draft the remediation action plan",
            description=(
                "Convert the analysis and research into a prioritized action plan: action, "
                "owner, expected impact, effort and risk. Clearly mark sensitive real-world "
                "actions (pricing, discounts, campaign launches, customer contact) as requiring "
                "human approval. Do not invent numbers or product names."
            ),
            agent=AgentRole.EXECUTION,
            depends_on=["task-2"],
            tool_hints=["report_writer"],
            sensitive=action_requested,
            expected_output="Prioritized action plan with owners, impact and sensitive flags.",
            success_criteria=[
                "Plan actions are specific, owned and prioritized",
                "Sensitive actions are explicitly marked",
            ],
            priority=3,
        ),
        AgentRole.REPORTING: TaskSpec(
            id="task-4",
            title="Compose the final business report",
            description=(
                "Synthesize research, analysis and action plan into an executive-ready Markdown "
                "business report with executive summary, findings & evidence, recommendations "
                "(owner/impact), risks & limitations/data gaps, sources and next actions."
            ),
            agent=AgentRole.REPORTING,
            depends_on=["task-2", "task-3"],
            tool_hints=["report_writer"],
            sensitive=False,
            expected_output="Full Markdown business report.",
            success_criteria=[
                "Complete Markdown report with all required sections",
                "Findings cite quantified evidence and sources",
                "Next actions, data gaps and limitations are stated",
            ],
            priority=2,
        ),
    }


def _canonicalize(plan: Plan, objective: str, action_requested: bool) -> Plan:
    """Collapse to exactly one task per specialist role with canonical dependencies."""
    templates = canonical_tasks(objective, action_requested=action_requested)
    by_role: dict[AgentRole, TaskSpec] = {}
    for task in plan.tasks:
        by_role.setdefault(task.agent, task)
    tasks: list[TaskSpec] = []
    for index, role in enumerate(CANONICAL_ORDER, start=1):
        task = by_role.get(role) or templates[role]
        template = templates[role]
        task.agent = role
        task.id = f"task-{index}"
        # Guarantee the tools each role needs to be useful.
        required_hint = template.tool_hints[0] if template.tool_hints else None
        if required_hint and required_hint not in task.tool_hints:
            task.tool_hints = [required_hint, *task.tool_hints]
        if not task.title:
            task.title = template.title
        if not task.description:
            task.description = template.description
        if not task.success_criteria:
            task.success_criteria = list(template.success_criteria)
        if not task.expected_output:
            task.expected_output = template.expected_output
        if role == AgentRole.EXECUTION:
            task.sensitive = action_requested
        tasks.append(task)
    research, analyst, execution, reporting = tasks
    research.depends_on = []
    analyst.depends_on = []
    execution.depends_on = [research.id, analyst.id]
    reporting.depends_on = [execution.id]
    plan.tasks = tasks
    return plan


def objective_requests_action(objective: str) -> bool:
    """True when the objective asks AgentForge to *do* something, not just report."""
    return bool(_ACTION_RE.search(objective or ""))


def map_agent(value: Any, *, title: str = "", description: str = "") -> AgentRole:
    """Map an arbitrary agent label to a supported role using aliases + keywords."""
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in _ALIASES:
        return _ALIASES[raw]
    for role in AgentRole:
        if raw == role.value:
            return role
    # Unknown: infer from the task text.
    haystack = f"{title} {description}".lower()
    if haystack.strip():
        for role, keywords in _ROLE_KEYWORDS.items():
            if any(keyword in haystack for keyword in keywords):
                return role
    return AgentRole.ANALYST


def _filter_tool_hints(hints: Iterable[str], known_tools: set[str] | None) -> list[str]:
    result: list[str] = []
    for hint in hints or []:
        name = str(hint).strip()
        if not name or name in result:
            continue
        if known_tools is not None and name not in known_tools:
            continue
        result.append(name)
    return result


def _topological_order(tasks: list[TaskSpec]) -> list[TaskSpec]:
    """Stable topological sort by ``depends_on``; cycles fall back to original order."""
    by_id = {task.id: task for task in tasks}
    for task in tasks:
        task.depends_on = [dep for dep in task.depends_on if dep in by_id and dep != task.id]
    scheduled: list[TaskSpec] = []
    scheduled_ids: set[str] = set()
    remaining = list(tasks)
    guard = 0
    while remaining and guard < len(tasks) + 4:
        guard += 1
        progressed = False
        for task in list(remaining):
            if all(dep in scheduled_ids for dep in task.depends_on):
                scheduled.append(task)
                scheduled_ids.add(task.id)
                remaining.remove(task)
                progressed = True
        if not progressed:
            # Dependency cycle: drop the offending edges and append in original order.
            for task in remaining:
                task.depends_on = []
            scheduled.extend(remaining)
            break
    return scheduled


def ensure_reporting_task(plan: Plan) -> Plan:
    if any(task.agent == AgentRole.REPORTING for task in plan.tasks):
        return plan
    last_ids = [task.id for task in plan.tasks[-2:]]
    plan.tasks.append(
        TaskSpec(
            id=f"task-{len(plan.tasks) + 1}",
            title="Compose the final business report",
            description=(
                "Synthesize all prior task results into an executive-ready Markdown business "
                "report with findings, evidence, recommendations (owner/impact), sources and "
                "limitations."
            ),
            agent=AgentRole.REPORTING,
            depends_on=last_ids,
            tool_hints=["report_writer", "knowledge_search"],
            expected_output="Full Markdown business report.",
            success_criteria=[
                "All prior findings are reflected with quantified evidence",
                "Recommendations include owners and expected impact",
                "Report is complete Markdown with sources and limitations",
            ],
            priority=2,
        )
    )
    return plan


def normalize_plan(
    plan: Plan,
    *,
    objective: str = "",
    known_tools: set[str] | None = None,
    max_tasks: int = 6,
) -> Plan:
    """Repair/validate a Plan so the workflow can execute it deterministically."""
    # 1) Filter unknown tools + dedupe ids.
    seen_ids: set[str] = set()
    for index, task in enumerate(plan.tasks, start=1):
        if not task.id or task.id in seen_ids:
            task.id = f"task-{index}"
        seen_ids.add(task.id)
        task.tool_hints = _filter_tool_hints(task.tool_hints, known_tools)
        if task.agent == AgentRole.PLANNER:  # not executable
            task.agent = map_agent("", title=task.title, description=task.description)

    # Analyst tools can retrieve company context themselves; don't block independent
    # SQL work just because a separate research task failed.
    research_ids = {task.id for task in plan.tasks if task.agent == AgentRole.RESEARCH}
    for task in plan.tasks:
        if task.agent == AgentRole.ANALYST:
            task.depends_on = [dep for dep in task.depends_on if dep not in research_ids]

    # 2) Trim (keep the reporting task) and guarantee one.
    if len(plan.tasks) > max_tasks:
        reporting_task = next(
            (task for task in reversed(plan.tasks) if task.agent == AgentRole.REPORTING),
            None,
        )
        others = [task for task in plan.tasks if task is not reporting_task]
        plan.tasks = others[: max_tasks - 1] + ([reporting_task] if reporting_task else [])
    plan = ensure_reporting_task(plan)

    # 3) Topological ordering by dependency.
    plan.tasks = _topological_order(plan.tasks)

    # 4) Sensitive-task policy.
    action_requested = objective_requests_action(objective or plan.objective)
    for task in plan.tasks:
        if task.agent != AgentRole.EXECUTION:
            task.sensitive = False
        elif action_requested:
            task.sensitive = True
        else:
            task.sensitive = False
    plan.requires_approval = any(task.sensitive for task in plan.tasks)
    if not plan.objective:
        plan.objective = objective
    if not plan.summary:
        plan.summary = (
            f"Execute {len(plan.tasks)} tasks: "
            + ", ".join(f"{task.id} ({task.agent.value})" for task in plan.tasks)
        )
    return plan


def build_plan_from_dict(
    raw: dict[str, Any],
    *,
    objective: str = "",
    known_tools: set[str] | None = None,
) -> Plan:
    """Validate LLM planner output, repairing unknown agents before validation."""
    payload = dict(raw or {})
    payload.setdefault("objective", objective)
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("plan has no tasks")
    repaired_tasks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            continue
        task = dict(item)
        task.setdefault("id", f"task-{index}")
        task.setdefault("title", f"Task {index}")
        task.setdefault("description", task.get("title") or "")
        role = map_agent(
            task.get("agent"),
            title=task.get("title", ""),
            description=task.get("description", ""),
        )
        task["agent"] = role.value
        if not isinstance(task.get("depends_on"), list):
            task["depends_on"] = []
        if not isinstance(task.get("tool_hints"), list):
            task["tool_hints"] = []
        if not isinstance(task.get("success_criteria"), list):
            task["success_criteria"] = []
        repaired_tasks.append(task)
    if not repaired_tasks:
        raise ValueError("plan has no valid task objects")
    payload["tasks"] = repaired_tasks
    plan = Plan.model_validate(payload)
    return normalize_plan(plan, objective=objective, known_tools=known_tools)


__all__ = [
    "build_plan_from_dict",
    "ensure_reporting_task",
    "map_agent",
    "normalize_plan",
    "objective_requests_action",
]
