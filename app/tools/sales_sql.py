"""``sales_sql`` — the core analyst tool: read-only SQL over the demo sales DB.

Safety is layered:

1. a statement whitelist (``SELECT`` / ``WITH ... SELECT`` / ``EXPLAIN``),
2. string/comment-aware scanning that rejects multiple statements (``;``) and any
   write/DDL keyword (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA, ...),
3. a **read-only SQLite connection** (``mode=ro`` + ``PRAGMA query_only=ON``) so a
   validation bug still cannot mutate the demo database,
4. a row cap (default/maximum 200) and a wall-clock interrupt via SQLite's
   progress handler so a pathological query cannot hang a workflow.

The tool is parameterless by design: agents generate the SQL, so there is no
injection surface from bound parameters and no environment-specific query
templating.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.tools._support import clamp_int, json_safe, resolve_sales_db
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolSpec

MAX_ROWS = 200
DEFAULT_ROWS = 200
QUERY_TIMEOUT_SECONDS = 8.0

_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|"
    r"pragma|vacuum|reindex|grant|revoke|savepoint|begin|commit|rollback|"
    r"load_extension|into\s+outfile)\b",
    re.IGNORECASE,
)
_READ_STARTERS = ("select", "with", "explain")


def _blank_strings_and_comments(sql: str) -> str:
    """Return SQL with literals/comments replaced by spaces (keyword-safe scan)."""
    out: list[str] = []
    index = 0
    length = len(sql)
    quote: str | None = None
    while index < length:
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < length and sql[index + 1] == quote:
                    index += 2
                    out.append(" ")
                    continue
                quote = None
            out.append(" ")
            index += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            out.append(" ")
            index += 1
            continue
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            while index < length and sql[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and sql[index + 1] == "*":
            index += 2
            while index + 1 < length and not (sql[index] == "*" and sql[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def validate_readonly_sql(sql: str) -> str:
    """Validate and normalize one read-only SQL statement.

    Returns the statement (without trailing semicolons) or raises ``ValueError``
    with a human-readable reason. The error messages are shown to agents, so they
    state exactly how to fix the query.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("query must be a non-empty SQL SELECT statement")
    if len(sql) > 20_000:
        raise ValueError("query is too long (limit 20,000 characters)")
    scrubbed = _blank_strings_and_comments(sql)
    statement = scrubbed.strip()
    while statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if not statement:
        raise ValueError("query must contain a SELECT statement")
    if ";" in statement:
        raise ValueError(
            "multiple SQL statements are not allowed; send exactly one SELECT statement"
        )
    first_token = re.match(r"([a-zA-Z]+)", statement)
    if not first_token or first_token.group(1).lower() not in _READ_STARTERS:
        raise ValueError(
            "only read-only SELECT queries are allowed (SELECT / WITH ... SELECT / EXPLAIN)"
        )
    match = _WRITE_KEYWORDS.search(statement)
    if match:
        raise ValueError(
            f"write/DDL keyword not allowed in a read-only query: '{match.group(0).strip()}'"
        )
    # Re-attach the original text up to the first semicolon so string literals and
    # comments are preserved exactly for SQLite. Trailing ';' strip is safe because
    # any ';' outside literals was rejected above.
    original = sql.strip()
    while original.endswith(";"):
        original = original[:-1].rstrip()
    return original


class SalesSqlTool(BaseTool):
    spec = ToolSpec(
        name="sales_sql",
        description=(
            "Run one read-only SQL SELECT against the AgentForge demo sales database "
            "(SQLite: products, sales, marketing_spend, support_tickets). Use it to "
            "answer quantitative questions such as revenue by product/region/month, "
            "gross margin, discount rate, returns and support-ticket counts. Inspect the "
            "schema with SELECT name FROM sqlite_master WHERE type='table'. Writes, DDL "
            "and multiple statements are rejected; results are capped at 200 rows."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A single read-only SQL SELECT statement.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows to return (1-200, default 200).",
                },
            },
            "required": ["query"],
        },
        scopes=("read:sales",),
        sensitive=False,
        category="data",
        timeout_seconds=20,
    )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        sql = args.get("query", "")
        try:
            statement = validate_readonly_sql(sql)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        limit = clamp_int(args.get("limit"), DEFAULT_ROWS, 1, MAX_ROWS)

        db_path = resolve_sales_db(ctx)
        if not db_path.exists():
            return ToolResult.failure(
                f"sales database not found at {db_path}; run scripts/generate_demo_data.py"
            )
        if db_path.is_dir():
            return ToolResult.failure(f"sales database path is a directory: {db_path}")

        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        connection: sqlite3.Connection | None = None
        started = time.monotonic()
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
            connection.execute("PRAGMA query_only=ON")
            deadline = started + QUERY_TIMEOUT_SECONDS

            def _interrupt() -> int:
                return 1 if time.monotonic() > deadline else 0

            connection.set_progress_handler(_interrupt, 10_000)
            cursor = connection.execute(statement)
            if cursor.description is None:
                return ToolResult.failure(
                    "query returned no result set; use a SELECT statement"
                )
            raw_columns = [str(description[0]) for description in cursor.description]
            fetched = cursor.fetchmany(limit + 1)
        except sqlite3.Error as exc:
            return ToolResult.failure(f"SQL error: {exc}")
        finally:
            if connection is not None:
                connection.close()

        truncated = len(fetched) > limit
        rows: list[dict[str, Any]] = []
        columns = _unique_columns(raw_columns)
        for raw_row in fetched[:limit]:
            rows.append(
                {column: json_safe(value) for column, value in zip(columns, raw_row)}
            )
        return ToolResult.success(
            {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "limit": limit,
            },
            meta={"database": Path(db_path).name, "truncated": truncated},
        )


def _unique_columns(raw_columns: list[str]) -> list[str]:
    """De-duplicate SQL result columns (e.g. two ``revenue`` aliases)."""
    seen: dict[str, int] = {}
    columns: list[str] = []
    for raw in raw_columns:
        name = raw or "column"
        count = seen.get(name, 0)
        seen[name] = count + 1
        columns.append(name if count == 0 else f"{name}_{count + 1}")
    return columns


TOOL = SalesSqlTool()

__all__ = ["SalesSqlTool", "TOOL", "validate_readonly_sql"]
