"""``report_writer`` — write a Markdown artifact under the run's artifacts directory.

The tool is non-sensitive (writing a report is the expected output of the
reporting agent) and confined to ``ToolContext.resolved_artifacts_dir``: names are
sanitized to a basename, traversal is stripped and the resolved destination is
verified to stay inside the artifacts root. Returns the artifact metadata the
orchestrator needs to persist an ``artifacts`` row.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.tools._support import sanitize_artifact_name, slugify
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolSpec

MAX_CONTENT_CHARS = 2_000_000


class ReportWriterTool(BaseTool):
    spec = ToolSpec(
        name="report_writer",
        description=(
            "Write a final Markdown deliverable (business report, analysis memo, "
            "remediation plan) to the run's artifacts directory and return its path and "
            "metadata. Use it for the reporting agent's final output so the orchestrator "
            "can persist the artifact and the dashboard can render it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Human-readable report title.",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown report body.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional filename (sanitized; .md enforced).",
                },
                "kind": {
                    "type": "string",
                    "description": "Artifact kind; markdown is the default.",
                },
                "description": {
                    "type": "string",
                    "description": "Short description for the artifact record.",
                },
            },
            "required": ["title", "content"],
        },
        scopes=("write:artifacts",),
        sensitive=False,
        category="artifact",
        timeout_seconds=20,
    )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = str(args.get("title") or "").strip()
        content = args.get("content")
        if not title:
            return ToolResult.failure("title is required and must be non-empty")
        if not isinstance(content, str) or not content.strip():
            return ToolResult.failure("content is required and must be non-empty Markdown")
        if len(content) > MAX_CONTENT_CHARS:
            return ToolResult.failure(
                f"content is too large ({len(content)} chars; limit {MAX_CONTENT_CHARS})"
            )
        kind = str(args.get("kind") or "markdown").strip().lower()
        description = str(args.get("description") or "").strip()

        artifacts_root = ctx.resolved_artifacts_dir
        requested_name = str(args.get("name") or "").strip()
        if requested_name:
            filename = sanitize_artifact_name(requested_name)
        else:
            filename = f"{slugify(title)}.md"
        destination = self._unique_path(artifacts_root, filename)
        try:
            resolved_root = artifacts_root.resolve()
            resolved_destination = destination.resolve()
            if resolved_destination.parent != resolved_root:
                return ToolResult.failure("artifact path escapes the artifacts directory")
        except OSError as exc:
            return ToolResult.failure(f"could not resolve artifact path: {exc}")

        body = content.rstrip() + "\n"
        # Prepend the title unless the body already opens with an H1 heading.
        if not re.match(r"^#\s+\S", body.lstrip()):
            body = f"# {title}\n\n{body}"
        try:
            resolved_destination.write_text(body, encoding="utf-8", newline="\n")
        except OSError as exc:
            return ToolResult.failure(f"could not write report: {exc}")

        size = resolved_destination.stat().st_size
        output = {
            "path": str(resolved_destination),
            "name": resolved_destination.name,
            "title": title,
            "kind": kind,
            "description": description,
            "chars": len(body),
            "bytes": size,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": ctx.run_id,
        }
        return ToolResult.success(output, meta={"artifact": resolved_destination.name, "bytes": size})

    @staticmethod
    def _unique_path(root, filename: str):
        """Return a non-conflicting destination inside ``root``."""
        candidate = root / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        for counter in range(2, 1000):
            alternative = root / f"{stem}-{counter}{suffix}"
            if not alternative.exists():
                return alternative
        return root / f"{stem}-{datetime.now(timezone.utc).strftime('%H%M%S')}{suffix}"


TOOL = ReportWriterTool()

__all__ = ["ReportWriterTool", "TOOL"]
