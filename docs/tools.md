# Tools — Design & Contracts

AgentForge agents act only through registered tools. Every tool is a small, typed,
side-effect-explicit capability with a `ToolSpec` (name, description, JSON input
schema, scopes, `sensitive`, category, timeout) and a `run(args, ctx) -> ToolResult`
implementation. The registry validates required arguments and primitive types,
enforces agent scopes and the approval gate, times execution, and never lets a tool
exception crash a workflow.

Source: `app/tools/base.py`, `app/tools/registry.py` (contracts — Lead), concrete
tools in `app/tools/{sales_sql,csv_query,knowledge_search,web_search,report_writer}.py`
(this deliverable). Tests: `tests/test_tools_rag.py`.

## 1. Registered tools at a glance

| Tool | Category | Scopes | `sensitive` | What it does |
|---|---|---|---|---|
| `sales_sql` | data | `read:sales` | no | One read-only SQL `SELECT` over the demo SQLite sales DB (≤200 rows). |
| `csv_query` | data | `read:sales` | no | Describe / aggregate / head the demo CSV mirrors without SQL. |
| `knowledge_search` | knowledge | `read:knowledge` | no | Hybrid RAG retrieval over internal memos with title/source/score. |
| `web_search` | web | `read:web` | no | **Offline** curated market-notes lookup, always marked `simulated: true`. |
| `report_writer` | artifact | `write:artifacts` | no | Writes a Markdown deliverable into the run's artifacts directory. |

All five tools are non-sensitive: writing an analysis report is the expected
outcome of the reporting agent, and the demo's human-approval scenario comes from a
`sensitive` *task flag* in the orchestrator, not from a destructive tool. The
registry's approval enforcement (`ctx.extras["approval_granted"]`) is ready for
action tools (email, campaign launch) if they are added later.

Discovery is lazy and deterministic: `get_registry()` imports the modules in
`DEFAULT_TOOL_MODULES` order (`sales_sql`, `csv_query`, `knowledge_search`,
`web_search`, `report_writer`) and registers each module's `TOOL` object. A broken
module is logged and skipped rather than crashing app startup. `app/tools/__init__.py`
re-exports `get_registry`.

## 2. `sales_sql` — the core analyst tool

**Input schema**

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "description": "A single read-only SQL SELECT statement."},
    "limit": {"type": "integer", "description": "Maximum rows to return (1-200, default 200)."}
  },
  "required": ["query"]
}
```

**Output**

```json
{
  "columns": ["name", "ttm_revenue"],
  "rows": [{"name": "Tide Portable Speaker", "ttm_revenue": 138612.78}],
  "row_count": 1,
  "truncated": false,
  "limit": 200
}
```

Rows are dicts keyed by (de-duplicated) column name; values are JSON-safe scalars
(`bytes` are decoded/hexed, non-finite floats become `null`).

**Safety layers** (defence in depth):

1. **Statement whitelist** — the first keyword must be `SELECT`, `WITH`, or `EXPLAIN`.
2. **String/comment-aware scan** — literals and comments are blanked before scanning,
   so `WHERE name = 'Aurora; Lamp'` is allowed while `SELECT 1; DELETE ...` is not.
   Multiple statements (`;` outside literals) and write/DDL keywords (`INSERT`,
   `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `PRAGMA`, `ATTACH`, `VACUUM`,
   `LOAD_EXTENSION`, ...) are rejected with an actionable error message.
3. **Read-only connection** — SQLite is opened as `file:...?mode=ro` and
   `PRAGMA query_only=ON`; even a validator bug cannot mutate the demo DB.
4. **Row cap + timeout** — 200 rows maximum; SQLite's progress handler interrupts a
   query after 8 seconds so a pathological `SELECT` cannot hang a run.
5. **No template injection** — the tool takes no bound parameters or table names from
   untrusted sources; agents generate complete SQL, which the validator inspects.

The DB path comes from `Settings.sales_db_path` (default
`data/demo/sales.db`), overridable in tests via `ctx.extras["sales_db_path"]`.

**Worked example — the demo objective**

```sql
SELECT p.name, ROUND(SUM(s.revenue), 2) AS ttm_revenue
FROM sales s
JOIN products p ON p.product_id = s.product_id
WHERE s.sale_date BETWEEN '2025-11-01' AND '2026-10-31'
GROUP BY p.product_id
ORDER BY ttm_revenue ASC
LIMIT 3;
```

Verified tool output: `Tide Portable Speaker` $138,612.78, `Cirrus Air Purifier`
$337,551.94, `Solstice Ergonomic Chair` $383,761.96 (next SKU, Lumen, is $597,900).
Useful catalogs: `products`, `sales`, `marketing_spend`, `support_tickets`
(schema in `docs/ARCHITECTURE.md` §6). `metrics_definitions.md` in the knowledge
corpus contains ready-to-use canonical SQL, including the bottom-SKU query above.

## 3. `csv_query` — read-only CSV aggregation

**Input schema**

```json
{
  "type": "object",
  "properties": {
    "file": {"type": "string", "description": "Basename under data/demo, e.g. monthly_product_metrics"},
    "operation": {"type": "string", "description": "describe (default) | aggregate | head"},
    "group_by": {"type": "string"},
    "metric": {"type": "string", "description": "count | sum | mean | min | max"},
    "column": {"type": "string", "description": "Numeric column for sum/mean/min/max"},
    "filter_column": {"type": "string"},
    "filter_value": {"type": "string"},
    "limit": {"type": "integer", "description": "1-200, default 20"}
  },
  "required": ["file"]
}
```

Available files: `products.csv` (8 rows), `monthly_product_metrics.csv` (144 rows:
product × month revenue/cost/margin/discount/returns/marketing), `region_month_metrics.csv`
(72 rows). Security: only basenames directly inside the demo data directory; paths,
`..` and absolute paths are rejected. Columns are validated against the CSV header;
numeric metrics require a detected numeric column. Maximum 100k rows scanned.

Output shape: `{"file", "operation", "columns", "numeric_columns", "total_rows",
"rows", "row_count"}`. Aggregate rows look like
`{"group": "Nimbus Wireless Headphones", "rows": 18, "sum(revenue)": 2535282.47}`.

## 4. `knowledge_search` — RAG retrieval

**Input schema**: `{"query": str required, "k"?: int (1-20, default `RAG_TOP_K`),
"min_score"?: number (default `RAG_MIN_SCORE` = 0.08, values above **0.25 are
capped**)}`.

The local hybrid scores are *relative relevance*, not calibrated probabilities.
An agent that guesses a strict threshold (e.g. `min_score: 0.5`) would otherwise get
zero passages and be tempted to invent sources, so the tool clamps the effective
threshold:

```
effective_min_score = max(RAG_MIN_SCORE, min(requested, 0.25))
```

The effective value, the requested value and `min_score_capped` are returned so the
behaviour is observable. The parameter remains in the schema (agents may loosen it
or leave the default), but prompts say "leave it at the default".

**Output**

```json
{
  "query": "why are Solstice chair returns increasing",
  "results": [
    {
      "chunk_id": "...", "doc_id": "...",
      "title": "Quality Escalation & Defect Management",
      "source": "quality_escalation.md",
      "text": "Quality Escalation & Defect Management > 4. Active escalation - Solstice ...",
      "score": 0.28,
      "metadata": {"headings": ["..."], "section": "...", "chunk_index": 7,
                   "score_components": {"cosine": 0.26, "lexical": 0.34}}
    }
  ],
  "count": 1, "k": 5,
  "min_score": 0.08, "requested_min_score": 0.08, "min_score_capped": false
}
```

Snippets are self-describing: every chunk text starts with its heading breadcrumb,
so an agent can cite `title`/`source` and quote the passage directly. Retrieval is
deterministic, local, and network-free (see `docs/rag.md`). When the store is empty
or nothing matches, the tool returns `ok=True`, `results: []` plus an actionable
`note` (broaden the query / leave `min_score` default / ingest command) instead of
failing the task. Empty results are a prompt to retry with different keywords, never
a licence to invent sources.

## 5. `web_search` — offline-safe external context

**Input schema**: `{"query": str required, "max_results"?: int (1-8, default 3)}`.

Never performs a network request. It ranks the curated corpus in
`data/knowledge/market/*.md` with the local deterministic embedder and returns:

```json
{
  "query": "ergonomic chair return rate benchmark",
  "results": [{"title": "External Market Note — Ergonomic Seating (2025–2026)",
                "url": "https://market-notes.agentforge.local/ergonomic-seating",
                "source": "ergonomic_chairs.md",
                "snippet": "**Coverage period:** 2025–2026 ...", "score": 0.24,
                "simulated": true, "period": "2025-2026"}],
  "count": 1, "simulated": true, "network": false,
  "corpus_period": "2025-2026", "as_of": "2026-10",
  "note": "Curated offline market notes covering 2025-2026 only ..."
}
```

The corpus is deliberately **date-consistent with the demo dataset (2025-05 →
2026-10)** and **product-agnostic**: documents describe categories and benchmarks
and never name internal SKUs, SKU ids or other product categories. Titles and body
text carry the 2025–2026 coverage label. `simulated: true` is always present
(top-level and per result) plus `network: false` and `corpus_period`, so agents and
reviewers can never mistake these notes for live web data or attribute them to a
specific internal product. If the corpus directory is missing it returns an empty,
clearly-labelled result set — it never hangs or raises. A production deployment can
add a real provider behind the same schema; the `simulated` flag should then be
`false`.

## 6. `report_writer` — deterministic deliverable artifact

**Input schema**

```json
{
  "type": "object",
  "properties": {
    "title": {"type": "string"},
    "content": {"type": "string", "description": "Markdown report body."},
    "name": {"type": "string", "description": "Optional filename; sanitized, .md enforced."},
    "kind": {"type": "string", "description": "Artifact kind; markdown default."},
    "description": {"type": "string"}
  },
  "required": ["title", "content"]
}
```

Writes UTF-8 Markdown under `ctx.resolved_artifacts_dir` (default
`ARTIFACTS_DIR`/`artifacts`) and returns the metadata the orchestrator persists into
the `artifacts` table:

```json
{
  "path": "C:\\...\\artifacts\\sales-underperformance-analysis.md",
  "name": "sales-underperformance-analysis.md",
  "title": "Sales Underperformance Analysis",
  "kind": "markdown",
  "description": "Q3 analysis",
  "chars": 4821, "bytes": 4902,
  "created_at": "2026-11-04T19:40:09+00:00",
  "run_id": "..."
}
```

Safety: filename is reduced to a sanitized basename (directory components and `..`
stripped, letters/digits/`-_.` only), the resolved destination is verified to stay
inside the artifacts root, collisions get a `-2` suffix instead of overwriting, an
H1 is prepended when the body does not start with one, and content is capped at 2 MB.

## 7. Error-handling conventions

* Validation / safety failures return `ToolResult(ok=False, error="<actionable message>")`;
  the registry records them as failed tool events and the agent can adapt.
* Unexpected exceptions are caught by the registry (workflow never crashes), logged,
  and returned as `ok=False` with `TypeName: message`.
* Success results carry `output` (JSON-serializable), `duration_ms` (set by the
  registry when not provided) and optional `meta` (e.g. `database`, `count`,
  `artifact`, `truncated`).

## 8. Extending the tool set

```python
from app.tools.base import BaseTool, ToolContext, ToolResult, ToolSpec

class MyTool(BaseTool):
    spec = ToolSpec(
        name="my_tool",
        description="What it does and when an agent should use it.",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        scopes=("read:sales",),
        sensitive=False,
        category="data",
    )

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.success({"echo": args["q"]})

TOOL = MyTool()
```

Register by adding the module to `DEFAULT_TOOL_MODULES` in
`app/tools/registry.py`. Add `sensitive=True` for irreversible actions and rely on
the orchestrator's approval gate; never invent a side-effecting tool without it.

## 9. Limitations (honest notes)

* `sales_sql` is SQLite-flavoured (`date('now', ...)`, `substr(...)`); production SQL
  may need dialect tweaks. Explicit date bounds are recommended for reproducibility.
* The 200-row cap keeps LLM observations small; agents must aggregate in SQL rather
  than dump rows. Multi-statement scripts are intentionally unsupported.
* `csv_query` supports one grouping dimension and exact-match filters only — enough
  for demo exploration, not a full query engine.
* `web_search` returns curated/simulated notes, not live data. It is labelled as such
  everywhere to avoid hallucinated "market facts".
* `knowledge_search` is scoped to the internal corpus; it will not answer questions
  outside it and returns an empty set with guidance rather than guessing.
