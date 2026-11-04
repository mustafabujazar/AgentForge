"""Research agent: internal knowledge (and optional web) retrieval, evidence-backed findings."""

from __future__ import annotations

from app.agents.base import AgentContext, BaseAgent
from app.schemas.workflow import AgentResult, AgentRole, TaskSpec


class ResearchAgent(BaseAgent):
    role = AgentRole.RESEARCH
    name = "research"
    description = (
        "Investigates context and root causes using internal knowledge memos and web "
        "research, returning evidence-backed findings with sources."
    )
    capabilities = (
        "internal knowledge retrieval",
        "root-cause investigation",
        "competitive and market context",
        "evidence citation",
    )
    allowed_tools = ("knowledge_search", "web_search")
    scopes = ("read:knowledge", "read:web")
    max_tool_steps = 3
    temperature = 0.2

    def role_instructions(self) -> str:
        return (
            "Investigate WHY, not just WHAT. The internal knowledge corpus contains business "
            "memos only (quality process, pricing policy, marketing playbook, support "
            "escalation, product notes, QBR, market notes) — it does NOT contain SQL schemas or "
            "product lists, so never search for 'sales dataset schema'. Run 2-3 targeted "
            "knowledge_search queries with business phrasing, e.g. 'returns quality issue "
            "product', 'discount dependency pricing policy', 'marketing spend campaign "
            "effectiveness', 'support tickets severity escalation'. Quote or paraphrase the "
            "relevant memos and cite the document title/source in the 'source' field. If a "
            "search returns no passages, try a different business query before concluding; if "
            "web_search is unavailable or offline, say so in a metric instead of inventing "
            "sources. Every finding needs evidence and a source."
        )

    def postprocess(self, result: AgentResult, ctx: AgentContext, task: TaskSpec) -> AgentResult:
        result.metrics.setdefault("sources", sorted({f.source for f in result.findings if f.source}))
        return result


__all__ = ["ResearchAgent"]
