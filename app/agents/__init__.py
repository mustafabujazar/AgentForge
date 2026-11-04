"""Specialized AgentForge agents.

Four role-specialized agents share a bounded ReAct loop implementation
(:class:`~app.agents.base.BaseAgent`) and differ in prompt, tools and scopes:

============  ===================================  ==============================
role          tools                                scopes
============  ===================================  ==============================
research      knowledge_search, web_search         read:knowledge, read:web
analyst       sales_sql, csv_query,               read:sales, read:knowledge
              knowledge_search
execution     report_writer (+ approved actions)   write:artifacts
reporting     report_writer, knowledge_search      read:knowledge, write:artifacts
============  ===================================  ==============================
"""

from __future__ import annotations

from app.agents.analyst import AnalystAgent
from app.agents.base import AgentContext, BaseAgent
from app.agents.execution import ExecutionAgent
from app.agents.reporting import ReportingAgent
from app.agents.research import ResearchAgent
from app.schemas.workflow import AgentRole

AGENT_CLASSES: dict[AgentRole, type[BaseAgent]] = {
    AgentRole.RESEARCH: ResearchAgent,
    AgentRole.ANALYST: AnalystAgent,
    AgentRole.EXECUTION: ExecutionAgent,
    AgentRole.REPORTING: ReportingAgent,
}

_INSTANCES: dict[AgentRole, BaseAgent] = {role: cls() for role, cls in AGENT_CLASSES.items()}


def get_agent(role: AgentRole | str) -> BaseAgent:
    """Return the singleton agent for a role (raises ``KeyError`` for unknown roles)."""
    if isinstance(role, str):
        role = AgentRole(role)
    return _INSTANCES[role]


def agent_catalog() -> list[dict[str, object]]:
    """Serializable catalog used by ``GET /api/agents``."""
    return [
        {
            "name": agent.name,
            "role": role.value,
            "description": agent.description,
            "capabilities": list(agent.capabilities),
            "tools": list(agent.allowed_tools),
        }
        for role, agent in _INSTANCES.items()
    ]


__all__ = [
    "AGENT_CLASSES",
    "AnalystAgent",
    "AgentContext",
    "BaseAgent",
    "ExecutionAgent",
    "ReportingAgent",
    "ResearchAgent",
    "agent_catalog",
    "get_agent",
]
