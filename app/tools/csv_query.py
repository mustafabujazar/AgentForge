"""``csv_query`` — safe, read-only aggregation over the demo CSV mirrors.

Only files directly inside the demo data directory are readable (no paths, no
``..``), and every column referenced by an operation must exist in the CSV
header. Supported operations:

* ``describe`` — schema + numeric summary of every column.
* ``aggregate`` — ``count`` / ``sum`` / ``mean`` / ``min`` / ``max`` with optional
  ``group_by`` and exact-match filter.
* ``head`` — first N rows.

This complements ``sales_sql`` for questions the analyst wants answered without
writing SQL (product-month metrics, region roll-ups).
"""

from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.tools._support import clamp_int, resolve_csv_dir
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolSpec

MAX_LIMIT = 200
MAX_ROWS_SCANNED = 100_000
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _to_number(value: Any) -> float | None:
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if not text or not _NUMBER_RE.match(text):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _round(value: float) -> float:
    return round(value, 4)


class CsvQueryTool(BaseTool):
    spec = ToolSpec(
        name="csv_query",
        description=(
            "Read-only describe/aggregate/head over the AgentForge demo CSV mirrors in "
            "data/demo (products.csv, monthly_product_metrics.csv, region_month_metrics.csv). "
            "Use it to inspect columns, sum or average metrics by a dimension, or sample rows "
            "without writing SQL. Only basenames under data/demo are readable; columns are "
            "validated against the CSV header."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": (
                        "CSV basename, e.g. 'monthly_product_metrics.csv' or "
                        "'monthly_product_metrics'."
                    ),
                },
                "operation": {
                    "type": "string",
                    "description": "describe (default) | aggregate | head",
                },
                "group_by": {
                    "type": "string",
                    "description": "Column to group by for aggregate (optional).",
                },
                "metric": {
                    "type": "string",
                    "description": "Aggregate metric: count | sum | mean | min | max.",
                },
                "column": {
                    "type": "string",
                    "description": "Numeric column for sum/mean/min/max.",
                },
                "filter_column": {
                    "type": "string",
                    "description": "Optional column for an exact-match row filter.",
                },
                "filter_value": {
                    "type": "string",
                    "description": "Value to match (string compare, case-insensitive).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum result rows (1-200, default 20).",
                },
            },
            "required": ["file"],
        },
        scopes=("read:sales",),
        sensitive=False,
        category="data",
        timeout_seconds=20,
    )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        csv_dir = resolve_csv_dir(ctx)
        path, error = self._resolve_file(csv_dir, args.get("file", ""))
        if error:
            return ToolResult.failure(error)
        try:
            headers, records = self._read_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            return ToolResult.failure(f"could not read CSV '{path.name}': {exc}")
        if not headers:
            return ToolResult.failure(f"CSV '{path.name}' has no header row")
        limit = clamp_int(args.get("limit"), 20, 1, MAX_LIMIT)
        operation = str(args.get("operation") or "describe").strip().lower()

        filter_error = self._validate_filter(headers, args)
        if filter_error:
            return ToolResult.failure(filter_error)
        if args.get("filter_column"):
            target = str(args["filter_value"]).strip().lower()
            records = [
                record
                for record in records
                if str(record.get(str(args["filter_column"]), "")).strip().lower() == target
            ]

        numeric_columns = self._numeric_columns(headers, records)
        base = {
            "file": path.name,
            "operation": operation,
            "columns": headers,
            "numeric_columns": numeric_columns,
            "total_rows": len(records),
        }
        try:
            if operation == "describe":
                rows = self._describe(headers, records, numeric_columns)
            elif operation == "aggregate":
                rows = self._aggregate(headers, records, numeric_columns, args, limit)
            elif operation == "head":
                rows = [self._typed_record(record, numeric_columns) for record in records[:limit]]
            else:
                return ToolResult.failure(
                    f"unsupported operation '{operation}'; use describe, aggregate or head"
                )
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success({**base, "rows": rows, "row_count": len(rows)})

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _resolve_file(csv_dir: Path, requested: str) -> tuple[Path, str | None]:
        raw = str(requested or "").strip()
        if not raw:
            return Path(), "file is required"
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts or len(candidate.parts) > 1:
            return Path(), "file must be a CSV basename inside the demo data directory"
        name = raw if raw.lower().endswith(".csv") else f"{raw}.csv"
        path = csv_dir / name
        if not path.is_file():
            available = sorted(item.name for item in csv_dir.glob("*.csv")) if csv_dir.is_dir() else []
            return Path(), f"CSV '{name}' not found; available: {available}"
        return path, None

    @staticmethod
    def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = [header.strip() for header in (reader.fieldnames or [])]
            records: list[dict[str, str]] = []
            for index, row in enumerate(reader):
                if index >= MAX_ROWS_SCANNED:
                    break
                records.append(
                    {
                        header: (row.get(header) or "").strip()
                        for header in headers
                    }
                )
            return headers, records

    @staticmethod
    def _validate_filter(headers: list[str], args: dict[str, Any]) -> str | None:
        filter_column = args.get("filter_column")
        if filter_column:
            if filter_column not in headers:
                return f"unknown filter column '{filter_column}'; columns: {headers}"
            if "filter_value" not in args:
                return "filter_value is required when filter_column is set"
        return None

    @staticmethod
    def _numeric_columns(headers: list[str], records: list[dict[str, str]]) -> list[str]:
        numeric: list[str] = []
        for header in headers:
            values = [record[header] for record in records if record[header] != ""]
            if not values:
                continue
            parsed = [_to_number(value) for value in values]
            if all(number is not None for number in parsed):
                numeric.append(header)
        return numeric

    @staticmethod
    def _typed_record(record: dict[str, str], numeric_columns: list[str]) -> dict[str, Any]:
        typed: dict[str, Any] = {}
        for key, value in record.items():
            if key in numeric_columns:
                number = _to_number(value)
                typed[key] = number if number is not None else value
            else:
                typed[key] = value
        return typed

    @staticmethod
    def _describe(
        headers: list[str],
        records: list[dict[str, str]],
        numeric_columns: list[str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for header in headers:
            values = [record[header] for record in records if record[header] != ""]
            row: dict[str, Any] = {
                "column": header,
                "type": "number" if header in numeric_columns else "text",
                "non_null": len(values),
                "unique": len(set(values)),
            }
            if header in numeric_columns:
                numbers = [number for number in (_to_number(value) for value in values) if number is not None]
                if numbers:
                    row.update(
                        {
                            "min": _round(min(numbers)),
                            "max": _round(max(numbers)),
                            "mean": _round(sum(numbers) / len(numbers)),
                        }
                    )
            rows.append(row)
        return rows

    def _aggregate(
        self,
        headers: list[str],
        records: list[dict[str, str]],
        numeric_columns: list[str],
        args: dict[str, Any],
        limit: int,
    ) -> list[dict[str, Any]]:
        metric = str(args.get("metric") or "sum").strip().lower()
        if metric not in {"count", "sum", "mean", "min", "max"}:
            raise ValueError(f"unsupported metric '{metric}'; use count, sum, mean, min or max")
        group_by = args.get("group_by")
        if group_by and group_by not in headers:
            raise ValueError(f"unknown group_by column '{group_by}'; columns: {headers}")
        column = args.get("column")
        if metric != "count":
            if not column:
                raise ValueError(f"column is required for metric '{metric}'")
            if column not in headers:
                raise ValueError(f"unknown column '{column}'; columns: {headers}")
            if column not in numeric_columns:
                raise ValueError(
                    f"column '{column}' is not numeric; numeric columns: {numeric_columns}"
                )
        groups: dict[str, list[float]] = defaultdict(list)
        counts: dict[str, int] = defaultdict(int)
        for record in records:
            key = str(record[group_by]) if group_by else "all"
            counts[key] += 1
            if metric != "count":
                number = _to_number(record[column])
                if number is not None:
                    groups[key].append(number)
        value_label = f"{metric}({column or 'rows'})"
        result: list[dict[str, Any]] = []
        for key, count in counts.items():
            row: dict[str, Any] = {"group": key, "rows": count}
            if metric == "count":
                row[value_label] = count
            else:
                numbers = groups.get(key, [])
                if not numbers:
                    row[value_label] = None
                elif metric == "sum":
                    row[value_label] = _round(sum(numbers))
                elif metric == "mean":
                    row[value_label] = _round(sum(numbers) / len(numbers))
                elif metric == "min":
                    row[value_label] = _round(min(numbers))
                else:
                    row[value_label] = _round(max(numbers))
            result.append(row)

        def _sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
            raw = item.get(value_label)
            numeric = float(raw) if isinstance(raw, (int, float)) else float("-inf")
            return (0 if isinstance(raw, (int, float)) else 1, -numeric, str(item["group"]).lower())

        result.sort(key=_sort_key)
        return result[:limit]


TOOL = CsvQueryTool()

__all__ = ["CsvQueryTool", "TOOL"]
