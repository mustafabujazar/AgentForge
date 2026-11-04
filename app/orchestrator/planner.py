"""LLM planner with a deterministic heuristic fallback.

The planner produces a :class:`~app.schemas.workflow.Plan` (3-6 tasks) assigning
the research / analyst / execution / reporting agents, declaring dependencies,
tool hints, expected outputs, success criteria and the human-approval requirement
for action-taking execution tasks.

If the LLM is unavailable, over budget, or returns invalid JSON, the deterministic
:func:`heuristic_plan` is used so a run always starts with a sane plan.
"""

from __future__ import annotations

import json
import logging

from app.llm.base import ChatMessage
from app.schemas.workflow import AgentRole, Plan, TaskSpec
from app.orchestrator.router import (
    build_plan_from_dict,
    normalize_plan,
    objective_requests_action,
)

logger = logging.getLogger(__name__)

PLANNER_MAX_TOKENS = 2000


_PLANNER_SYSTEM = """You are the PLANNER agent in AgentForge, a governed multi-agent business automation platform.

Break the user objective into 3-6 concrete tasks executed by these agents:
- research  : internal knowledge/memo + web retrieval, root-cause evidence (tools: knowledge_search, web_search)
- analyst   : quantitative SQL/CSV analysis of the sales dataset (tools: sales_sql, csv_query, knowledge_search)
- execution : turns findings into a prioritized remediation/action plan (tool: report_writer)
- reporting : composes the final Markdown business report (tools: report_writer, knowledge_search)

RULES:
- Return ONLY one JSON object: {"objective","summary","tasks":[...],"success_criteria":[...],"requires_approval":bool}
- 3-6 tasks. Keep tasks specific and independent where possible. Research and SQL
  analysis can run independently unless one task explicitly needs the other's output.
- Task fields: id ("task-1"...), title, description (what to do + what to compute/retrieve), agent, depends_on (list of task ids), tool_hints (only from: sales_sql, csv_query, knowledge_search, web_search, report_writer), expected_output, success_criteria (2-3 short bullets), priority (1=highest..5).
- Ordering: research and analyst before execution; exactly one reporting task LAST,
  depending on execution and any tasks whose results it needs.
- sensitive MUST be false for research/analyst/reporting and for pure analysis/report objectives.
- sensitive=true ONLY for an EXECUTION task when the objective explicitly asks to ACT: launch/execute/send/email/refund/update/create campaign/add discount/deploy/publish/schedule/notify/implement. If the user just asks to analyze, investigate or report, set sensitive=false everywhere.
- Success criteria must be measurable and tied to the objective.

Be terse. JSON only."""


class Planner:
    """Plan generator; injected LLM keeps it testable."""

    def __init__(self, runtime: object, known_tools: set[str] | None = None) -> None:
        self.runtime = runtime
        self.known_tools = known_tools

    # -- public -----------------------------------------------------------
    def create_plan(self, objective: str) -> tuple[Plan, str]:
        """Return ``(plan, source)`` where source is ``"llm"`` or ``"heuristic"``."""
        llm = getattr(self.runtime, "llm", None)
        if llm is not None and not getattr(llm, "exhausted", False):
            raw = self._invoke_llm(objective)
            if raw is not None:
                try:
                    plan = build_plan_from_dict(
                        raw, objective=objective, known_tools=self.known_tools
                    )
                    if len(plan.tasks) >= 3:
                        return plan, "llm"
                    logger.warning(
                        "planner_plan_too_small",
                        extra={"tasks": len(plan.tasks), "objective": objective[:120]},
                    )
                except Exception as exc:
                    logger.warning(
                        "planner_plan_invalid",
                        extra={"error": str(exc), "objective": objective[:120]},
                    )
        plan = heuristic_plan(objective)
        return normalize_plan(plan, objective=objective, known_tools=self.known_tools), "heuristic"

    # -- LLM --------------------------------------------------------------
    def _invoke_llm(self, objective: str) -> dict | None:
        from app.llm.base import extract_json

        messages = [
            ChatMessage("system", _PLANNER_SYSTEM),
            ChatMessage("user", f"OBJECTIVE:\n{objective}\n\nReturn the plan JSON now."),
        ]
        for attempt in range(2):
            try:
                response = self.runtime.llm.invoke(
                    messages,
                    temperature=getattr(self.runtime.settings, "planner_temperature", 0.1),
                    max_tokens=PLANNER_MAX_TOKENS + (800 if attempt else 0),
                    json_mode=True,
                    agent="planner",
                )
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.warning(
                    "planner_llm_failed",
                    extra={"error": self.last_error, "attempt": attempt},
                )
                if attempt == 0 and ("HTTP 400" in str(exc) or "Failed to validate JSON" in str(exc)):
                    messages.append(
                        ChatMessage(
                            "user",
                            "IMPORTANT: your previous completion was rejected as invalid/"
                            "truncated JSON. Reply with ONE complete minified JSON object only, "
                            "close every brace, and keep descriptions to one sentence.",
                        )
                    )
                    continue
                return None
            try:
                data = extract_json(response.content or "")
            except Exception as exc:
                self.last_error = f"LLMResponseError: {exc}"[:500]
                logger.warning(
                    "planner_llm_unparseable",
                    extra={"error": self.last_error, "attempt": attempt},
                )
                if attempt == 0:
                    messages.append(
                        ChatMessage(
                            "user",
                            "Your reply was not parseable JSON. Reply with the plan JSON only "
                            "(minified, no markdown fences).",
                        )
                    )
                    continue
                return None
            if isinstance(data, dict):
                return data
            if attempt == 0:
                messages.append(ChatMessage("user", "Reply with ONE JSON object only."))
        return None


def heuristic_plan(objective: str) -> Plan:
    """Deterministic 4-task plan: research → analyst → execution → reporting."""
    wants_action = objective_requests_action(objective)
    tasks = [
        TaskSpec(
            id="task-1",
            title="Investigate root causes and internal context",
            description=(
                "Retrieve internal memos and market notes relevant to the objective "
                "(quality, pricing/discounting, marketing, support escalation, product notes). "
                "Explain likely causes of under/over-performance with citations."
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
        TaskSpec(
            id="task-2",
            title="Quantify performance with SQL analysis",
            description=(
                "Query the demo sales database and quantify trailing-12-month revenue, units, "
                "gross margin, period-over-period deltas, return rates, average discounts, "
                "marketing spend and support-ticket load by product. Rank products to surface "
                "the worst performers."
            ),
            agent=AgentRole.ANALYST,
            depends_on=[],
            tool_hints=["sales_sql", "csv_query", "knowledge_search"],
            sensitive=False,
            expected_output="Quantified product ranking with metrics and YoY deltas.",
            success_criteria=[
                "Trailing-12m revenue by product computed with SQL",
                "Period-over-period deltas and return/discount metrics computed",
                "Worst-performing products identified with concrete numbers",
            ],
            priority=1,
        ),
        TaskSpec(
            id="task-3",
            title="Draft the remediation action plan",
            description=(
                "Convert the analysis and research into a prioritized action plan: action, "
                "owner, expected impact, effort and risk. Clearly mark sensitive real-world "
                "actions (pricing, discounts, campaign launches, customer contact) as requiring "
                "human approval."
            ),
            agent=AgentRole.EXECUTION,
            depends_on=["task-1", "task-2"],
            tool_hints=["report_writer"],
            sensitive=wants_action,
            expected_output="Prioritized action plan with owners, impact and sensitive flags.",
            success_criteria=[
                "Plan actions are specific, owned and prioritized",
                "Sensitive actions are explicitly marked",
            ],
            priority=3,
        ),
        TaskSpec(
            id="task-4",
            title="Compose the final business report",
            description=(
                "Synthesize research, analysis and action plan into an executive-ready Markdown "
                "business report with executive summary, findings & evidence, recommendations "
                "(owner/impact), risks & limitations, sources and next actions."
            ),
            agent=AgentRole.REPORTING,
            depends_on=["task-3"],
            tool_hints=["report_writer", "knowledge_search"],
            sensitive=False,
            expected_output="Full Markdown business report.",
            success_criteria=[
                "Complete Markdown report with all required sections",
                "Findings cite quantified evidence and sources",
                "Next actions and limitations are stated",
            ],
            priority=2,
        ),
    ]
    return Plan(
        objective=objective,
        summary=(
            "Research internal context, quantify performance with SQL, draft a remediation "
            "plan, and synthesize an executive business report."
        ),
        tasks=tasks,
        success_criteria=[
            "Identify the worst-performing products with quantified evidence",
            "Explain root causes with internal/market sources",
            "Provide prioritized recommendations with owners and expected impact",
            "Deliver a complete, readable Markdown report",
        ],
        requires_approval=wants_action,
    )


__all__ = ["Planner", "heuristic_plan"]


# Keep ``json`` import used by type checkers/tools that introspect the module.
_ = json
