"""Analyst agent: SQL/CSV data analysis of the demo sales dataset."""

from __future__ import annotations

from app.agents.base import AgentContext, BaseAgent
from app.schemas.workflow import AgentResult, AgentRole, TaskSpec

_SQL_SCHEMA = """SQLite demo dataset (read-only). Tables:
- products(product_id INTEGER, name TEXT, category TEXT, unit_price REAL, unit_cost REAL, launch_date TEXT)
- sales(sale_id INTEGER, product_id INTEGER, region TEXT, channel TEXT, quantity INTEGER, revenue REAL, cost REAL, discount REAL, returned INTEGER, sale_date TEXT 'YYYY-MM-DD')
- marketing_spend(campaign_id TEXT, product_id INTEGER, month TEXT 'YYYY-MM', spend REAL)
- support_tickets(ticket_id INTEGER, product_id INTEGER, issue_type TEXT, severity TEXT, opened_at TEXT)
Use ONLY SELECT statements on `sales_sql` with args {"query": "<SELECT ...>"}."""


class AnalystAgent(BaseAgent):
    role = AgentRole.ANALYST
    name = "analyst"
    description = (
        "Queries the sales database to quantify performance, trends, returns, discounts, "
        "marketing spend and support burden, producing figures with evidence."
    )
    capabilities = (
        "SQL analytics",
        "revenue and margin trend analysis",
        "YoY / period-over-period deltas",
        "return-rate and discount diagnostics",
        "marketing-spend and ticket correlation",
    )
    allowed_tools = ("sales_sql", "csv_query", "knowledge_search")
    scopes = ("read:sales", "read:knowledge")
    max_tool_steps = 3
    temperature = 0.1

    def role_instructions(self) -> str:
        return (
            "You MUST query the data before concluding — do not answer from intuition. "
            "Run at least 2-3 SQL queries on sales_sql (combine multiple metrics per query where "
            "possible to stay within the tool budget). Minimum analysis to cover:\n"
            "1) trailing-12-month revenue, units and gross margin by product (use MAX(sale_date) as "
            "'today'; window = last 12 months); rank products best→worst.\n"
            "2) period-over-period change: compare the trailing 12 months with the prior 12 months "
            "for revenue, and (if data is shorter than 24 months) state the partial window used.\n"
            "3) return rate, average discount and gross-margin% per product for the trailing window.\n"
            "4) marketing spend per product (join marketing_spend by product_id/month) and "
            "support tickets by product/severity.\n"
            f"{_SQL_SCHEMA}\n"
            "Findings must cite concrete numbers in 'evidence' and name the tool/source "
            "(e.g. 'sales_sql: trailing-12m revenue by product')."
        )

    def postprocess(self, result: AgentResult, ctx: AgentContext, task: TaskSpec) -> AgentResult:
        result.metrics.setdefault("has_sql_evidence", any(r.kind == "sql" for r in []))
        result.metrics["tool_call_count"] = len(result.tool_calls)
        result.metrics["sql_calls"] = [r.tool for r in result.tool_calls if r.ok]
        return result


__all__ = ["AnalystAgent"]
