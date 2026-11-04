"""Task evaluation: deterministic rules + LLM judge, combined into a retry decision.

The evaluator is deliberately strict on evidence: an agent that claims conclusions
without tool-backed findings scores poorly even if the prose sounds good.  The LLM
judge can only move the score around the deterministic baseline; hard rule failures
always fail the task (which triggers the retry loop).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.llm.base import ChatMessage, extract_json
from app.schemas.workflow import AgentResult, AgentRole, Evaluation, TaskSpec, TaskStatus

logger = logging.getLogger(__name__)

EVALUATOR_MAX_TOKENS = 800

_EVALUATOR_SYSTEM = """You are the EVALUATOR agent in AgentForge. Judge one completed task result against its success criteria.

Return ONLY one JSON object:
{"score": <0.0-1.0>, "passed": <bool>, "criteria_met": ["..."], "issues": ["..."], "feedback": "<= 2 terse sentences of concrete improvement advice"}

Rubric:
1. Does the result directly answer the task with specific content (not generic filler)?
2. Is every major claim backed by evidence (numbers, memo quotes, tool outputs)? Invented data = fail.
3. Are the success criteria met?
4. Is the result complete enough to hand to the next agent?

Be strict but fair; JSON only."""

_QUANTIFIED_CLAIM_RE = re.compile(
    r"(?<![\w-])(?P<currency>[$€£]\s*)?"
    r"(?P<number>-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?P<unit>\s*(?:%|percent|[kmb](?![A-Za-z])))?",
    re.IGNORECASE,
)
_SOURCE_NUMBER_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def unsupported_quantified_claims(text: str, source_text: str) -> list[str]:
    """Find explicit numeric claims unsupported by the supplied evidence."""
    source_numbers = [
        float(match.replace(",", ""))
        for match in _SOURCE_NUMBER_RE.findall(source_text)
    ]
    unsupported: list[str] = []
    for match in _QUANTIFIED_CLAIM_RE.finditer(text):
        number_text = match.group("number")
        unit = (match.group("unit") or "").strip().lower()
        if not (match.group("currency") or unit or "," in number_text or "." in number_text):
            continue
        number = float(number_text.replace(",", ""))
        scale = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
        candidates = [number * scale[unit]] if unit in scale else [number]
        if unit in {"%", "percent"}:
            candidates.append(number / 100)
        tolerance = max(0.02, abs(number) * 0.01, 0.55 if unit in {"%", "percent"} else 0)
        if not any(
            abs(candidate - source) <= max(
                tolerance,
                abs(candidate) * 0.01,
            )
            for candidate in candidates
            for source in source_numbers
        ):
            unsupported.append(match.group(0).strip())
    return unsupported


def _unsupported_quantified_claims(result: AgentResult) -> list[str]:
    """Find explicit numeric claims absent from successful data-tool outputs."""
    data_tools = {"sales_sql", "csv_query", "knowledge_search", "web_search"}
    source_text = "\n".join(
        call.output_preview
        for call in result.tool_calls
        if call.ok and call.tool in data_tools and call.output_preview
    )
    claim_text = "\n".join(
        f"{finding.statement} {finding.evidence}" for finding in result.findings
    )
    return unsupported_quantified_claims(claim_text, source_text)


class Evaluator:
    def __init__(self, runtime: object) -> None:
        self.runtime = runtime
        self.settings = runtime.settings
        self.last_judge_error: str | None = None

    # -- public -----------------------------------------------------------
    def evaluate(
        self,
        task: TaskSpec,
        result: AgentResult | None,
        *,
        attempt: int = 1,
    ) -> Evaluation:
        rule_score, hard_ok, criteria_met, issues = self._rules(task, result)
        llm_judge: dict[str, Any] | None = None
        clearly_passing = rule_score >= 0.92 and hard_ok and not issues
        if (
            result is not None
            and not clearly_passing
            and getattr(self.runtime.llm, "exhausted", False) is False
        ):
            llm_judge = self._judge(task, result)
        if llm_judge is not None:
            try:
                llm_score = max(0.0, min(1.0, float(llm_judge.get("score", rule_score))))
            except (TypeError, ValueError):
                llm_score = rule_score
            score = round(0.55 * rule_score + 0.45 * llm_score, 3)
            llm_passed = bool(llm_judge.get("passed", False))
            for item in llm_judge.get("criteria_met") or []:
                if isinstance(item, str) and item and item not in criteria_met:
                    criteria_met.append(item)
            for item in llm_judge.get("issues") or []:
                if isinstance(item, str) and item and item not in issues:
                    issues.append(item)
            feedback = str(llm_judge.get("feedback") or "").strip()
            if not feedback:
                feedback = "; ".join(issues[:3]) or "Improve evidence, completeness and criteria coverage."
            evaluator_kind = "llm+rules"
            passed = bool(hard_ok and llm_passed and score >= self.settings.eval_pass_score)
        else:
            score = rule_score
            if self.last_judge_error:
                judge_issue = f"LLM judge unavailable: {self.last_judge_error}"
                if judge_issue not in issues:
                    issues.append(judge_issue)
            feedback = "; ".join(issues[:3]) or "Task meets the deterministic requirements."
            evaluator_kind = "rules"
            passed = bool(hard_ok and score >= self.settings.eval_pass_score)
        should_retry = (not passed) and attempt <= int(self.settings.max_agent_retries)
        return Evaluation(
            task_id=task.id,
            score=score,
            passed=passed,
            criteria_met=criteria_met[:8],
            issues=issues[:8],
            feedback=feedback[:1200],
            should_retry=should_retry,
            evaluator=evaluator_kind,
        )

    # -- deterministic rules ---------------------------------------------
    def _rules(
        self,
        task: TaskSpec,
        result: AgentResult | None,
    ) -> tuple[float, bool, list[str], list[str]]:
        checks: list[tuple[str, float, bool]] = []
        criteria_met: list[str] = []
        issues: list[str] = []

        def check(name: str, weight: float, ok: bool, *, issue: str | None = None) -> bool:
            checks.append((name, weight, ok))
            if ok:
                criteria_met.append(name)
            elif issue:
                issues.append(issue)
            return ok

        if result is None:
            issues.append("agent produced no result")
            return 0.0, False, criteria_met, issues

        completed = result.status == TaskStatus.COMPLETED
        check("result completed", 2.0, completed, issue="agent result status is not completed")
        if result.error:
            check("no agent error", 1.0, False, issue=f"agent error: {result.error[:200]}")
        else:
            check("no agent error", 1.0, True)

        summary = (result.summary or "").strip()
        check(
            "informative summary",
            1.0,
            len(summary) >= 40,
            issue="summary too short to be useful",
        )

        findings = result.findings or []
        evidence_findings = [
            finding
            for finding in findings
            if (finding.evidence or "").strip() or (finding.source or "").strip()
        ]
        role = task.agent
        if role == AgentRole.RESEARCH:
            check(
                "research findings cite sources",
                2.5,
                len(evidence_findings) >= 3,
                issue="research needs >= 3 findings with evidence/source",
            )
            check(
                "source attribution",
                1.0,
                sum(1 for f in findings if f.source.strip()) >= 2,
                issue="at least two findings must name a source document",
            )
            unsupported_claims = _unsupported_quantified_claims(result)
            check(
                "quantified claims grounded in tool outputs",
                2.0,
                not unsupported_claims,
                issue=(
                    "quantified claims are not present in successful tool outputs: "
                    + ", ".join(unsupported_claims[:6])
                    if unsupported_claims
                    else None
                ),
            )
        elif role == AgentRole.ANALYST:
            check(
                "data findings quantified",
                2.5,
                len(evidence_findings) >= 2,
                issue="analyst needs >= 2 findings with numeric evidence",
            )
            unsupported_claims = _unsupported_quantified_claims(result)
            check(
                "quantified claims grounded in tool outputs",
                2.5,
                not unsupported_claims,
                issue=(
                    "quantified claims are not present in successful tool outputs: "
                    + ", ".join(unsupported_claims[:6])
                    if unsupported_claims
                    else None
                ),
            )
            check(
                "SQL/data tool used",
                1.5,
                any(
                    call.ok and call.tool in {"sales_sql", "csv_query"}
                    for call in result.tool_calls
                ),
                issue="analyst did not successfully call a data tool",
            )
        elif role == AgentRole.EXECUTION:
            plan_md = str(result.metrics.get("artifact_markdown") or "")
            check(
                "action plan present",
                2.0,
                len(plan_md) >= 400 or bool(result.artifacts),
                issue="execution agent must produce a substantive action plan",
            )
            check(
                "actionable findings",
                1.0,
                len(findings) >= 1,
                issue="action plan has no listed actions",
            )
        elif role == AgentRole.REPORTING:
            report_md = str(result.metrics.get("artifact_markdown") or "")
            check(
                "report artifact present",
                2.5,
                len(report_md) >= 800 or bool(result.artifacts),
                issue="reporting agent must produce a full Markdown report",
            )
            check(
                "report sections",
                1.0,
                "#" in report_md or bool(result.artifacts),
                issue="report lacks Markdown structure",
            )

        if task.tool_hints:
            successful_tools = {call.tool for call in result.tool_calls if call.ok}
            check(
                "requested tools exercised",
                1.5,
                bool(successful_tools.intersection(task.tool_hints)),
                issue=f"task tool hints {task.tool_hints} but no hinted tool succeeded",
            )
        check(
            "confidence reported",
            0.5,
            result.confidence >= 0.2,
            issue="agent confidence is very low",
        )

        total_weight = sum(weight for _, weight, _ in checks) or 1.0
        score = round(sum(weight for _, weight, ok in checks if ok) / total_weight, 3)
        critical = {
            "result completed",
            "informative summary",
            "research findings cite sources",
            "source attribution",
            "data findings quantified",
            "quantified claims grounded in tool outputs",
            "SQL/data tool used",
            "requested tools exercised",
            "action plan present",
            "report artifact present",
        }
        hard_ok = all(ok for name, _, ok in checks if name in critical) and not result.error
        return score, hard_ok, criteria_met, issues

    # -- LLM judge --------------------------------------------------------
    def _judge(self, task: TaskSpec, result: AgentResult) -> dict[str, Any] | None:
        digest = {
            "task": {
                "id": task.id,
                "title": task.title,
                "agent": task.agent.value,
                "success_criteria": task.success_criteria,
            },
            "result": {
                "status": result.status.value,
                "summary": (result.summary or "")[:1500],
                "findings": [
                    {
                        "statement": finding.statement[:400],
                        "evidence": finding.evidence[:400],
                        "source": finding.source[:120],
                        "confidence": finding.confidence,
                    }
                    for finding in result.findings[:8]
                ],
                "tool_calls": [
                    {"tool": call.tool, "ok": call.ok, "error": (call.error or "")[:150]}
                    for call in result.tool_calls[:8]
                ],
                "confidence": result.confidence,
                "artifact_chars": len(str(result.metrics.get("artifact_markdown") or "")),
            },
        }
        messages = [
            ChatMessage("system", _EVALUATOR_SYSTEM),
            ChatMessage("user", json.dumps(digest, default=str)[:7000]),
        ]
        try:
            response = self.runtime.llm.invoke(
                messages,
                temperature=0.0,
                max_tokens=EVALUATOR_MAX_TOKENS,
                json_mode=True,
                agent="evaluator",
            )
            data = extract_json(response.content or "")
            if not isinstance(data, dict):
                return None
            if "score" not in data and "passed" not in data:
                # Provider returned JSON that is not a judge verdict (e.g. mock LLM):
                # treat the judge as unavailable and fall back to deterministic rules.
                return None
            return data
        except Exception as exc:
            self.last_judge_error = f"{type(exc).__name__}: {exc}"[:400]
            logger.warning(
                "evaluator_llm_failed",
                extra={"error": self.last_judge_error, "task_id": task.id},
            )
            return None


__all__ = ["Evaluator"]
