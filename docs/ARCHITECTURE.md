# AgentForge — Architecture & Engineering Contracts

This document is the source of truth for module boundaries and shared interfaces.
All agents implement against it. Do not change a contract without updating this file.

## 1. Product

AgentForge turns a natural-language business request into a governed multi-agent
execution: **plan → route → execute tools → evaluate → retry → (human approval) →
synthesize deliverable**, with a complete auditable trace.

Example request:

> Analyze our sales performance, identify the three worst-performing products,
> investigate why they're underperforming, and generate a report with recommendations.

## 2. Stack (actually installed — do not add Python dependencies)

Python 3.12 · FastAPI · LangGraph 1.x · LangChain Core · SQLAlchemy 2.x · Pydantic v2 ·
httpx · pytest · python-dotenv · LangGraph `MemorySaver` checkpointer.
No numpy in the venv: RAG embeddings/cosine are pure-stdlib and deterministic.

- **Database:** PostgreSQL 16 + pgvector in production (Docker), SQLite in local dev.
  The same SQLAlchemy models run on both.
- **LLM:** provider abstraction. `groq` (default, live), `openai`, `ollama`, `mock`.
- **Frontend:** React + Vite + TypeScript, built to static assets served by FastAPI.
- **No new pip packages.** Use stdlib for JWT (HMAC), password hashing (`hashlib.scrypt`).

## 3. Directory layout & ownership

```
app/
  main.py                 # FastAPI app factory, routers, static mount       [api-db]
  core/
    config.py             # Settings, env loading                            [lead]
    logging.py            # structured logging                              [lead]
    security.py           # scrypt passwords, HS256 JWT                     [lead]
    events.py             # in-process event broker for SSE                 [lead]
    errors.py             # domain errors + handlers                        [lead]
  schemas/
    auth.py run.py workflow.py                                              [lead]
  llm/                    # provider abstraction, Groq/OpenAI/Ollama/Mock    [lead]
  db/
    base.py models.py     # engine, session, ORM models                     [lead]
    seed.py               # demo users/docs bootstrap                       [api-db]
  api/                    # auth, runs, events/SSE, approvals, knowledge,   [api-db]
                          # agents, tools, health routers, deps
  orchestrator/           # LangGraph graph, planner, router, executor,      [orchestrator]
                          # evaluator, approval, synthesizer, service
  agents/                 # research / analyst / execution / reporting      [orchestrator]
  tools/
    base.py registry.py   # ToolSpec/ToolContext/ToolResult, registry        [lead]
    sales_sql.py csv_query.py knowledge_search.py web_search.py
    report_writer.py      # concrete tools                                   [tools-rag]
  rag/                    # chunking, embeddings, vector store, ingestion    [tools-rag]
scripts/                  # generate_demo_data.py, benchmark.py, demo_run.py [lead/qa]
data/                     # demo SQLite sales DB + knowledge corpus          [lead]
frontend/                 # React/Vite dashboard                             [frontend]
tests/                    # pytest unit + integration + e2e                  [qa]
docs/                     # architecture, API contract, deployment, testing  [lead/qa]
Dockerfile docker-compose.yml .github/ Makefile .env.example                 [devops]
```

## 4. Core interfaces (frozen)

### 4.1 LLM (`app/llm`)

```python
from app.llm import get_llm, ChatMessage
llm = get_llm()                                  # cached provider from settings
resp = llm.invoke([ChatMessage("system", "..."), ChatMessage("user", "...")],
                  json_mode=True, temperature=0.1, max_tokens=1500)
resp.content      # str
resp.usage        # TokenUsage(prompt_tokens, completion_tokens, total_tokens, model, provider)
resp.latency_ms
```

`get_llm()` never raises at import; only on first network call. Providers: `groq`
(default), `openai`, `ollama`, `mock`. Errors: `LLMError`, `LLMTimeoutError`,
`LLMResponseError`. `extract_json(text) -> dict` is available from `app.llm`.

### 4.2 Tools (`app/tools`)

```python
from app.tools.registry import get_registry, ToolSpec, ToolContext, ToolResult

class MyTool:
    spec = ToolSpec(name="sales_sql", description="...",
                    input_schema={"type": "object", "properties": {...}, "required": [...]},
                    scopes=("read:sales",), sensitive=False, category="data")
    def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...

registry = get_registry()
registry.list_specs()                       # list[ToolSpec]
registry.get("sales_sql")                   # BaseTool | None
result = registry.execute("sales_sql", {"query": "SELECT ..."}, ctx)
# ToolResult(ok: bool, output: Any, error: str|None, duration_ms: int, meta: dict)

@dataclass
class ToolContext:
    run_id: str
    agent: str
    settings: Settings
    artifacts_dir: Path
    db_session_factory: Callable[[], Session]   # SQLAlchemy sessionmaker
    extras: dict = {}
```

Tools must be read-only unless `sensitive=True`. Sensitive tools only run after an
approved `Approval` row. The orchestrator enforces this; the registry does not.

### 4.3 RAG (`app/rag`)

```python
from app.rag.store import get_knowledge_store
store = get_knowledge_store()
store.ingest_text(title, source, text, metadata=None) -> int      # chunks added
store.ingest_directory(path) -> int
store.search(query, k=5, min_score=0.0) -> list[RetrievedChunk]
# RetrievedChunk: chunk_id, doc_id, title, source, text, score, metadata
```

Default embeddings: deterministic local hashing/TF-IDF hybrid implemented with the
Python standard library (no network, no numpy, no new deps). Documents persist in the
`knowledge_chunks` table so retrieval works across restarts. pgvector is the
production path; the local store uses pure-Python cosine similarity over persisted
vectors.

### 4.4 Orchestrator (`app/orchestrator/service.py`)

```python
from app.orchestrator.service import workflow_service

await workflow_service.start_run(run_id: str, objective: str)  # background task
await workflow_service.run_sync(objective, user_id=None)       # returns run_id, waits
await workflow_service.resume_run(run_id: str)                 # after approval
workflow_service.cancel(run_id: str)
```

The service owns DB writes for `runs`, `task_records`, `approvals`, `artifacts`,
`run_events` and emits the event stream via `app.core.events.broker`.

### 4.5 Event stream (`app/core/events.py`)

```python
from app.core.events import broker
broker.publish_threadsafe(run_id, event_dict)   # called from worker threads
async for event in broker.subscribe(run_id): ...
```

Every event dict: `{seq, run_id, type, ts, agent, task_id, message, data, duration_ms}`.
`seq` is monotonic per run. The DB is the durable log; the broker is the live tail.

Event types: `run.created`, `run.started`, `plan.created`, `task.started`,
`tool.called`, `agent.completed`, `evaluation.completed`, `task.retry`,
`approval.required`, `approval.resolved`, `report.created`, `run.completed`,
`run.failed`, `run.cancelled`, `run.awaiting_approval`.

## 5. Workflow

```
START → intake → plan → dispatch ──────────────────────────────┐
                     ▲                                          │
                     │ selected task                            │
                  route ─→ execute ─→ evaluate ─→ retry (≤2) ───┘
                     │             │
                     │             └→ (sensitive) approval gate → execute
                     ↓ all tasks done
                 synthesize → finalize → END
```

- **Planner** produces `Plan` (structured JSON): objective, 3–6 `TaskSpec`s with agent
  assignment, dependencies, tool hints, `sensitive` flag, success criteria.
- **Router** validates/repairs agent assignment from capability registry.
- **Executor** runs one task with the assigned agent, injecting relevant prior
  results, tools, and RAG context. Timeout + retry.
- **Evaluator** (LLM judge + deterministic checks) scores 0–1, returns issues and
  feedback; tasks below `EVAL_PASS_SCORE` retry up to `MAX_AGENT_RETRIES` with
  feedback appended.
- **Approval gate**: sensitive execution tasks pause the run (`awaiting_approval`),
  persist an `Approval`; `POST /api/runs/{id}/approvals/{aid}` resumes or rejects.
- **Synthesizer** composes the final Markdown business report from task results,
  evaluations, sources and artifacts; saved as an `Artifact` + `runs.final_report`.

State is a `TypedDict` (`app/schemas/workflow.py`), JSON-serializable, and checkpointed
by LangGraph `MemorySaver` keyed by `run_id`. Full run state is also snapshotted into
`runs.state_json` after every node so runs remain inspectable and resumable.

## 6. Demo dataset (generated by `scripts/generate_demo_data.py`)

`data/demo/sales.db` (SQLite):

- `products(product_id, name, category, unit_price, unit_cost, launch_date)`
- `sales(sale_id, product_id, region, channel, quantity, revenue, cost, discount,
  returned, sale_date)`
- `marketing_spend(campaign_id, product_id, month, spend)`
- `support_tickets(ticket_id, product_id, issue_type, severity, opened_at)`

Three products are deliberately underperforming with distinct causal stories
(quality issues, price/discount dependency, weak marketing). The demo objective must
be answerable from this data.

`data/knowledge/*.md` — internal memos (pricing policy, quality process, marketing
playbook, support escalation, product notes) ingested into the RAG store.

## 7. Config (`app/core/config.py`, `.env`)

`SECRET_KEY`, `DATABASE_URL` (sqlite or postgres), `LLM_PROVIDER`, `LLM_MODEL`,
`GROQ_API_KEY`, `LLM_TEMPERATURE`, `MAX_AGENT_RETRIES`, `EVAL_PASS_SCORE`,
`HITL_ENABLED`, `SALES_DB_PATH`, `KNOWLEDGE_DIR`, `ARTIFACTS_DIR`, `CORS_ORIGINS`,
`DEMO_USER_EMAIL`, `DEMO_USER_PASSWORD`.

## 8. Quality gates (definition of done)

1. `pytest` green, including a full mocked-LLM workflow and a real tool-backed run.
2. `python scripts/demo_run.py` completes a real Groq-backed run and writes a report.
3. Dashboard builds (`npm run build`) and its API client matches `/api`.
4. Docker image + compose validate (`docker compose config`), Postgres/pgvector path documented.
5. README explains architecture, decisions, benchmarks, and limitations honestly.
