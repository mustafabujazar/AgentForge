"""Final synthesis: compose the deliverable Markdown report, persist it as an artifact.

The LLM writes the report; a deterministic template fallback guarantees a complete,
honest report even when the model fails, the budget is exhausted or it returns
prose without structure.  Both paths include sources, limitations and next actions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.base import slugify
from app.llm.base import ChatMessage
from app.orchestrator import persistence
from app.orchestrator.evaluator import unsupported_quantified_claims
from app.orchestrator.runtime import RunRuntime
from app.schemas.workflow import AgentRole

logger = logging.getLogger(__name__)

SYNTHESIZER_MAX_TOKENS = 2800

_SYNTHESIZER_SYSTEM = """You are the SYNTHESIZER agent in AgentForge. You write the FINAL deliverable business report for executives.

Return ONLY the Markdown document (no JSON, no code fences around the whole document).

Required structure:
# <clear report title>
## Executive Summary
## Key Findings & Evidence
## Recommendations  (use a Markdown table: Recommendation | Owner | Expected Impact | Priority)
## Risks & Limitations
## Sources
## Next Actions

Rules:
- Use ONLY facts/numbers from the provided task results; never invent data.
- Cite where each major finding came from (task id / source).
- Be concise but complete: 700-1500 words.
- The report must stand alone for a reader who did not see the run."""


class Synthesizer:
    def __init__(self, runtime: RunRuntime) -> None:
        self.runtime = runtime
        self.settings = runtime.settings
        self.last_error: str | None = None

    # -- public -----------------------------------------------------------
    def synthesize(self, state: dict[str, Any]) -> dict[str, Any]:
        report, source = self._compose(state)
        artifact = self._persist(state, report)
        report = self._ensure_required_sections(report, state)
        # Re-persist after section guarantees so the artifact matches the run report.
        if artifact is not None:
            artifact["chars"] = len(report)
            try:
                Path(artifact["path"]).write_text(report, encoding="utf-8")
            except OSError:  # pragma: no cover
                logger.exception("synthesizer_artifact_rewrite_failed")
            try:
                persistence.update_artifact_content(artifact["id"], report)
            except Exception:  # pragma: no cover - persistence helper best effort
                logger.exception("synthesizer_artifact_update_failed")
        self.runtime.emit(
            "report.created",
            agent=AgentRole.REPORTING.value,
            message="Final business report created",
            data={
                "artifact_id": artifact.get("id") if artifact else None,
                "artifact_path": artifact.get("path") if artifact else None,
                "artifact_name": artifact.get("name") if artifact else None,
                "chars": len(report),
                "source": source,
            },
        )
        return {
            "final_report": report,
            "status": "completed",
            "context": {**(state.get("context") or {}), "artifact": artifact or {}, "report_source": source},
        }

    # -- LLM composition --------------------------------------------------
    def _compose(self, state: dict[str, Any]) -> tuple[str, str]:
        if getattr(self.runtime.llm, "exhausted", False):
            return self._template_report(state), "template"
        if self._has_unaccepted_tasks(state):
            return self._template_report(state), "template"
        payload = self._compact_context(state)
        messages = [
            ChatMessage("system", _SYNTHESIZER_SYSTEM),
            ChatMessage(
                "user",
                "Draft the final report from this run data:\n"
                + json.dumps(payload, default=str)[:14000],
            ),
        ]
        try:
            response = self.runtime.llm.invoke(
                messages,
                temperature=0.3,
                max_tokens=SYNTHESIZER_MAX_TOKENS,
                json_mode=False,
                agent="synthesizer",
            )
            text = (response.content or "").strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("markdown"):
                    text = text[len("markdown") :].lstrip("\n")
            if getattr(response, "finish_reason", None) == "length" and len(text) < 1200:
                self.last_error = (
                    "LLMResponseError: synthesizer output was truncated by the token limit "
                    f"({len(text)} chars)"
                )
                logger.warning(
                    "synthesizer_output_truncated",
                    extra={"run_id": self.runtime.run_id, "chars": len(text)},
                )
                return self._template_report(state), "template"
            if len(text) >= 600 and "#" in text:
                unsupported = unsupported_quantified_claims(
                    text,
                    self._accepted_evidence_text(state),
                )
                if unsupported:
                    self.last_error = (
                        "LLMResponseError: report contains ungrounded quantified claims: "
                        + ", ".join(unsupported[:8])
                    )
                    logger.warning(
                        "synthesizer_ungrounded_claims",
                        extra={
                            "run_id": self.runtime.run_id,
                            "claims": unsupported[:8],
                        },
                    )
                    return self._template_report(state), "template"
                return text, "llm"
            self.last_error = f"LLMResponseError: synthesizer output too short ({len(text)} chars)"
            logger.warning(
                "synthesizer_output_too_short",
                extra={"run_id": self.runtime.run_id, "chars": len(text)},
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:500]
            logger.warning(
                "synthesizer_llm_failed",
                extra={"error": self.last_error, "run_id": self.runtime.run_id},
            )
        return self._template_report(state), "template"

    def _accepted_evidence_text(self, state: dict[str, Any]) -> str:
        plan = state.get("plan") or {}
        results = state.get("results") or {}
        evaluations = state.get("evaluations") or {}
        evidence: list[str] = []
        for task in plan.get("tasks") or []:
            if task.get("agent") == "reporting" or not self._task_accepted(
                task, results, evaluations
            ):
                continue
            result = results.get(task.get("id")) or {}
            for finding in result.get("findings") or []:
                if isinstance(finding, dict):
                    evidence.extend(
                        [
                            str(finding.get("statement") or ""),
                            str(finding.get("evidence") or ""),
                        ]
                    )
            for call in result.get("tool_calls") or []:
                if (
                    isinstance(call, dict)
                    and call.get("ok")
                    and call.get("tool") != "report_writer"
                ):
                    evidence.append(str(call.get("output_preview") or ""))
        return "\n".join(evidence)

    def _has_unaccepted_tasks(self, state: dict[str, Any]) -> bool:
        results = state.get("results") or {}
        evaluations = state.get("evaluations") or {}
        return any(
            not self._task_accepted(task, results, evaluations)
            for task in (state.get("plan") or {}).get("tasks") or []
        )

    @staticmethod
    def _task_accepted(
        task: dict[str, Any],
        results: dict[str, Any],
        evaluations: dict[str, Any],
    ) -> bool:
        task_id = task.get("id")
        result = results.get(task_id) or {}
        evaluation = evaluations.get(task_id) or {}
        return result.get("status") == "completed" and evaluation.get("passed") is not False

    def _compact_context(self, state: dict[str, Any]) -> dict[str, Any]:
        plan = state.get("plan") or {}
        results = state.get("results") or {}
        evaluations = state.get("evaluations") or {}
        tasks: list[dict[str, Any]] = []
        for task in plan.get("tasks") or []:
            task_id = task.get("id")
            result = results.get(task_id) or {}
            evaluation = evaluations.get(task_id) or {}
            findings = []
            for finding in (result.get("findings") or [])[:8]:
                if not isinstance(finding, dict):
                    continue
                findings.append(
                    {
                        "statement": str(finding.get("statement") or "")[:400],
                        "evidence": str(finding.get("evidence") or "")[:400],
                        "source": str(finding.get("source") or "")[:150],
                        "confidence": finding.get("confidence", 0.5),
                    }
                )
            metrics = result.get("metrics") or {}
            tasks.append(
                {
                    "id": task_id,
                    "title": task.get("title"),
                    "agent": task.get("agent"),
                    "status": result.get("status"),
                    "summary": str(result.get("summary") or "")[:1200],
                    "findings": findings,
                    "artifact_names": [a.get("name") for a in (result.get("artifacts") or [])],
                    "artifact_markdown": str(metrics.get("artifact_markdown") or "")[:3000],
                    "error": result.get("error"),
                    "evaluation": {
                        "score": evaluation.get("score"),
                        "passed": evaluation.get("passed"),
                        "issues": (evaluation.get("issues") or [])[:4],
                    },
                }
            )
        return {
            "objective": state.get("objective"),
            "plan_summary": plan.get("summary"),
            "success_criteria": plan.get("success_criteria") or [],
            "tasks": tasks,
            "token_usage": self.runtime.token_usage(),
        }

    # -- deterministic fallback ------------------------------------------
    def _template_report(self, state: dict[str, Any]) -> str:
        objective = str(state.get("objective") or "Business analysis").strip()
        plan = state.get("plan") or {}
        results = state.get("results") or {}
        evaluations = state.get("evaluations") or {}
        tasks = plan.get("tasks") or []
        title = f"Business Analysis Report: {objective[:110]}"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        completed, failed = [], []
        for task in tasks:
            if self._task_accepted(task, results, evaluations):
                completed.append(task)
            else:
                failed.append(task)
        confidence_values = [
            float((results.get(task.get("id")) or {}).get("confidence") or 0.0) for task in tasks
            if self._task_accepted(task, results, evaluations)
        ]
        avg_confidence = (
            sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
        )
        execution_result = next(
            (
                results.get(t.get("id")) or {}
                for t in tasks
                if t.get("agent") == "execution"
                and self._task_accepted(t, results, evaluations)
            ),
            {},
        )
        analyst_result = next(
            (
                results.get(t.get("id")) or {}
                for t in tasks
                if t.get("agent") == "analyst"
                and self._task_accepted(t, results, evaluations)
            ),
            {},
        )
        reporting_result = next(
            (
                results.get(t.get("id")) or {}
                for t in tasks
                if t.get("agent") == "reporting"
                and self._task_accepted(t, results, evaluations)
            ),
            {},
        )
        summary = (
            (reporting_result.get("summary") or "").strip()
            or (analyst_result.get("summary") or "").strip()
            or (
                f"Only {len(completed)} of {len(tasks)} planned tasks passed evaluation. "
                "This report includes accepted results only and should be treated as partial."
                if failed
                else f"AgentForge executed {len(tasks)} tasks against the objective."
            )
        )

        lines: list[str] = [
            f"# {title}",
            "",
            f"_Generated by AgentForge on {now} · run `{self.runtime.run_id}` · "
            f"{len(completed)}/{len(tasks)} tasks completed · "
            f"average confidence {avg_confidence:.2f}_",
            "",
            "## Executive Summary",
            "",
            summary,
            "",
        ]

        lines.append("## Key Findings & Evidence")
        lines.append("")
        any_findings = False
        for task in tasks:
            if not self._task_accepted(task, results, evaluations):
                continue
            result = results.get(task.get("id")) or {}
            findings = result.get("findings") or []
            if not findings:
                continue
            any_findings = True
            lines.append(f"### {task.get('title')} ({task.get('agent')})")
            lines.append("")
            for finding in findings[:8]:
                if not isinstance(finding, dict):
                    continue
                statement = str(finding.get("statement") or "").strip()
                evidence = str(finding.get("evidence") or "").strip()
                source = str(finding.get("source") or "").strip()
                confidence = finding.get("confidence", 0.5)
                suffix = f" (source: {source}; confidence {confidence})" if source else f" (confidence {confidence})"
                lines.append(f"- **{statement}** — {evidence}{suffix}")
            lines.append("")
        if not any_findings:
            lines.append("- No structured findings were produced; see task summaries in the appendix.")
            lines.append("")

        lines.append("## Recommendations")
        lines.append("")
        execution_findings = execution_result.get("findings") or []
        if execution_findings:
            for finding in execution_findings[:8]:
                if isinstance(finding, dict):
                    lines.append(
                        f"- **{finding.get('statement')}** — {finding.get('evidence', '')}"
                    )
        else:
            lines.append(
                "- Review the worst-performing products, validate root causes with owners, and "
                "prioritize fixes in the next planning cycle."
            )
        lines.append("")
        lines.append("| Recommendation | Owner | Expected Impact | Priority |")
        lines.append("|---|---|---|---|")
        lines.append(
            "| Address the top underperforming products with targeted remediation | "
            "Product / Sales | Recover revenue and margin in the next quarter | High |"
        )
        lines.append(
            "| Instrument quality, pricing and marketing metrics for early warning | "
            "Operations / Analytics | Faster detection of future underperformance | Medium |"
        )
        lines.append("")

        lines.append("## Risks & Limitations")
        lines.append("")
        if self.last_error:
            if "ungrounded quantified claims" in self.last_error:
                lines.append(
                    "- The LLM report included quantitative claims not supported by accepted "
                    "evidence and was rejected; this report was assembled deterministically."
                )
            else:
                lines.append(
                    f"- The LLM report was rejected ({self.last_error}); this report was "
                    "assembled deterministically from the task results."
                )
        if failed:
            for task in failed:
                result = results.get(task.get("id")) or {}
                lines.append(
                    f"- Task `{task.get('id')}` ({task.get('agent')}) did not complete: "
                    f"{result.get('error') or 'unknown error'}."
                )
        else:
            lines.append("- All planned tasks completed successfully.")
        for task in tasks:
            evaluation = evaluations.get(task.get("id")) or {}
            for issue in (evaluation.get("issues") or [])[:2]:
                lines.append(f"- Evaluator note on `{task.get('id')}`: {issue}")
        lines.append(
            "- Analysis is based on the demo sales dataset and internal knowledge corpus; "
            "figures are historical and not forecasts."
        )
        lines.append("")

        lines.append("## Sources")
        lines.append("")
        sources: set[str] = set()
        accepted_ids = {
            task.get("id")
            for task in tasks
            if self._task_accepted(task, results, evaluations)
        }
        for task_id in accepted_ids:
            result = results.get(task_id) or {}
            for finding in (result or {}).get("findings") or []:
                if isinstance(finding, dict) and finding.get("source"):
                    sources.add(str(finding["source"]))
        if sources:
            for source in sorted(sources)[:12]:
                lines.append(f"- {source}")
        else:
            lines.append("- Demo sales database (`data/demo/sales.db`) and internal knowledge corpus.")
        lines.append("")

        lines.append("## Next Actions")
        lines.append("")
        lines.append("- Validate the findings and recommendations with the owning teams.")
        lines.append("- Convert high-priority recommendations into tracked initiatives with deadlines.")
        lines.append("- Re-run the analysis after remediation to measure impact.")
        lines.append("")

        lines.append("## Appendix: Run Metadata")
        lines.append("")
        lines.append("| Task | Agent | Status | Eval score |")
        lines.append("|---|---|---|---|")
        for task in tasks:
            result = results.get(task.get("id")) or {}
            evaluation = evaluations.get(task.get("id")) or {}
            lines.append(
                f"| {task.get('id')} | {task.get('agent')} | {result.get('status', 'missing')} | "
                f"{evaluation.get('score', 'n/a')} |"
            )
        usage = self.runtime.token_usage()
        lines.append("")
        lines.append(
            f"LLM calls: {usage.get('calls', 0)} · tokens: {usage.get('total_tokens', 0)} · "
            f"model: {usage.get('model') or 'n/a'} ({usage.get('provider') or 'n/a'})"
        )
        return "\n".join(lines)

    # -- persistence ------------------------------------------------------
    def _persist(self, state: dict[str, Any], report: str) -> dict[str, Any] | None:
        try:
            artifacts_dir = self.settings.artifacts_path
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            file_name = f"{self.runtime.run_id[:8]}-{slugify(str(state.get('objective') or 'report'))}.md"
            path = artifacts_dir / file_name
            path.write_text(report, encoding="utf-8")
            artifact_id = persistence.create_artifact(
                self.runtime.run_id,
                name=file_name,
                description="Final synthesized business report",
                kind="markdown",
                path=str(path),
                content=report,
            )
            return {
                "id": artifact_id,
                "name": file_name,
                "path": str(path),
                "kind": "markdown",
                "chars": len(report),
            }
        except Exception:  # pragma: no cover - report must still return to the run
            logger.exception("artifact_persist_failed", extra={"run_id": self.runtime.run_id})
            return None

    def _ensure_required_sections(self, report: str, state: dict[str, Any]) -> str:
        """Append missing required sections so every report is complete/auditable."""
        text = report.strip()
        lower = text.lower()
        additions: list[str] = []
        if "## limitations" not in lower and "## risks & limitations" not in lower:
            additions.append(
                "## Risks & Limitations\n\n- Analysis is based on the demo dataset and internal "
                "knowledge corpus; figures are historical.\n"
            )
        if "## sources" not in lower:
            plan = state.get("plan") or {}
            results = state.get("results") or {}
            evaluations = state.get("evaluations") or {}
            sources = sorted(
                {
                    str(finding.get("source"))
                    for task in (plan.get("tasks") or [])
                    if self._task_accepted(task, results, evaluations)
                    for result in [results.get(task.get("id")) or {}]
                    for finding in (result or {}).get("findings") or []
                    if isinstance(finding, dict) and finding.get("source")
                }
            )
            body = "\n".join(f"- {source}" for source in sources[:12]) or (
                "- Demo sales database and internal knowledge corpus."
            )
            additions.append(f"## Sources\n\n{body}\n")
        if "## next actions" not in lower:
            additions.append(
                "## Next Actions\n\n- Validate findings with owners and convert recommendations "
                "into tracked initiatives.\n"
            )
        if additions:
            text = text + "\n\n" + "\n\n".join(additions)
        return text


__all__ = ["Synthesizer"]
