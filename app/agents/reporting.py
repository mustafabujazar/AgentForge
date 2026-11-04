"""Reporting agent: composes a full Markdown business report from prior results."""

from __future__ import annotations

from app.agents.base import AgentContext, BaseAgent
from app.schemas.workflow import AgentResult, AgentRole, TaskSpec


class ReportingAgent(BaseAgent):
    role = AgentRole.REPORTING
    name = "reporting"
    description = (
        "Synthesizes the research, analysis and action plan into an executive-ready "
        "Markdown business report with evidence, recommendations and sources."
    )
    capabilities = (
        "business writing",
        "executive summary",
        "evidence synthesis",
        "recommendations with owners and impact",
        "source attribution",
    )
    allowed_tools = ("report_writer", "knowledge_search")
    scopes = ("read:knowledge", "write:artifacts")
    max_tool_steps = 2
    temperature = 0.3

    def role_instructions(self) -> str:
        return (
            "Write the FULL business report in Markdown inside 'artifact_markdown'. Required "
            "sections: '# <title>', '## Executive Summary', '## Key Findings & Evidence' "
            "(quantified, cite prior task evidence), '## Recommendations' (table with owner, "
            "action, expected impact, priority), '## Risks & Limitations', '## Sources', "
            "'## Next Actions'. Use concrete numbers from prior task results; never invent "
            "figures. Prefer tables and short bullets. Then call report_writer ONCE with "
            'args {"title": "...", "content": "<the full markdown>"} so the report is persisted, '
            "and finish with action='final' (include artifact_markdown too)."
        )

    def postprocess(self, result: AgentResult, ctx: AgentContext, task: TaskSpec) -> AgentResult:
        markdown = str(result.metrics.get("artifact_markdown") or "")
        has_md_artifact = any(artifact.name.endswith(".md") for artifact in result.artifacts)
        if markdown and not has_md_artifact:
            artifact = self.write_artifact(
                ctx,
                task,
                markdown,
                suffix="report",
                description="Business report drafted by the reporting agent.",
            )
            if artifact is not None:
                result.artifacts.append(artifact)
        if markdown:
            result.metrics["report_chars"] = len(markdown)
            result.metrics["report_sections"] = [
                line.lstrip("# ").strip()
                for line in markdown.splitlines()
                if line.startswith("## ")
            ][:12]
        return result


__all__ = ["ReportingAgent"]
