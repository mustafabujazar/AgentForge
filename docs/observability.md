# AgentForge — Observability & Auditability

AgentForge is a multi-agent system: when a run misbehaves, the question is
usually *"which agent, which tool, which retry, and how many tokens?"* This
guide maps the built-in telemetry to the operational questions, and documents
the optional LangSmith / OpenTelemetry hooks.

Related: [`docs/deployment.md`](deployment.md),
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md) §4.5 (events),
[`docs/API_CONTRACT.md`](API_CONTRACT.md) (metrics/health endpoints).

---

## 1. Structured logs

`app/core/logging.py` installs a single stdout handler at startup:

- `LOG_JSON=true` (default, container-friendly) → **one JSON object per line**:
  ```json
  {"ts":"2026-11-04T19:31:02.481+00:00","level":"INFO","logger":"agentforge.orchestrator.service","message":"task_completed","run_id":"9f2c…","task_id":"task_3","agent":"analyst","duration_ms":4120}
  ```
  Fields: `ts` (UTC ISO-8601), `level`, `logger`, `message`, plus any `extra=`
  fields passed by the caller (e.g. `run_id`, `agent`, `task_id`, `tool`,
  `duration_ms`, `tokens`) and `exception` with the stack trace on errors.
- `LOG_JSON=false` → human-readable single lines for local development.
- `LOG_LEVEL` (`DEBUG`…`ERROR`) sets the root level; noisy third-party loggers
  (`httpx`, `httpcore`, `uvicorn.access`, SQLAlchemy engine) are pinned to
  `WARNING` to keep the signal high.

Operational queries:

```powershell
# follow logs
docker compose logs -f backend

# only warnings/errors (JSON lines: filter on the level field)
docker compose logs backend | Select-String '"level":"(WARNING|ERROR)"'

# find everything for one run (works because run_id is an extra field)
docker compose logs backend | Select-String '"run_id":"<run-id>"'
```

In a real deployment, ship stdout to Loki/CloudWatch/Datadog; the JSON shape is
already ingestion-ready. The Compose json-file driver is capped at 10 MB × 3
files per service.

> Secrets: `app/db/base.py` redacts credentials when logging `DATABASE_URL`
> (`scheme://***@host/...`). Never log API keys or full prompts containing
> credentials. Keep `LLM_*`/`GROQ_*` values out of error messages.

---

## 2. `run_events` — the durable audit trail

Every meaningful step is persisted to `run_events` **and** published to the
in-process broker for live SSE. The database is the source of truth (survives
restarts and is queryable); the broker is only the live tail.

`RunEvent` columns (`app/db/models.py`):

| Column | Meaning |
|---|---|
| `id` | autoincrement row id |
| `run_id` | FK to `runs.id` (cascade delete) |
| `seq` | monotonic per-run sequence; unique `(run_id, seq)` |
| `type` | event type (see below) |
| `agent` | agent name, if any |
| `task_id` | plan task id, if any |
| `message` | human-readable summary |
| `data_json` | structured payload (plan, tool args/results, scores, errors) |
| `duration_ms` | step duration where applicable |
| `created_at` | UTC timestamp |

Event types: `run.created`, `run.started`, `plan.created`, `task.started`,
`tool.called`, `agent.completed`, `evaluation.completed`, `task.retry`,
`approval.required`, `approval.resolved`, `report.created`, `run.completed`,
`run.failed`, `run.cancelled`, `run.awaiting_approval`.

Access paths:

- UI timeline: `GET /api/runs/{run_id}/events/stream?after_seq=N` (SSE; heartbeat
  comment `: ping` every ~15 s, closes on terminal status; reconnect with
  `after_seq`).
- Polling fallback: `GET /api/runs/{run_id}/events?after_seq=N`.
- Direct SQL for forensics:
  ```powershell
  docker compose exec postgres psql -U agentforge -d agentforge `
    -c "SELECT seq, type, agent, task_id, duration_ms, created_at FROM run_events WHERE run_id='<id>' ORDER BY seq;"
  ```

Because `seq` is monotonic, an SSE reconnect with `after_seq` replays exactly
what the client missed — no gaps, no duplicates. The broker drops the oldest
queued event for a *slow live consumer* (queue full), but the durable log still
contains it; the polling fallback recovers it.

**Audit uses:** reconstruct the full decision path (plan → tools → evaluations →
retries → approvals → report), prove that a sensitive action was approved by a
human, and compare agent/tool behaviour across runs.

---

## 3. Token accounting and cost

The LLM layer returns `TokenUsage` (`app/llm/base.py`):

```python
LLMResponse(content=..., model=..., provider=..., usage=TokenUsage(
    prompt_tokens=..., completion_tokens=..., total_tokens=...), latency_ms=...)
```

Accounting flow:

1. Each LLM call's usage is recorded.
2. The orchestrator accumulates per-run usage into `runs.token_usage_json`,
   exposed as `RunRead.token_usage`:
   `{prompt_tokens, completion_tokens, total_tokens, calls}`.
3. `GET /api/metrics/runs` aggregates success rate, durations, tokens and
   retries across runs (dashboard metrics panel).
4. `evaluation_metrics` stores per-task `score`, `passed`, `attempts` and
   `latency_ms` for benchmarking.

Cost formula (apply current provider prices; do not hardcode them):

```
run_cost = prompt_tokens/1e6 * prompt_price_per_mtok
         + completion_tokens/1e6 * completion_price_per_mtok
```

Budget guards to watch: `LLM_MAX_CALLS_PER_RUN`, `MAX_AGENT_RETRIES`,
`EVAL_PASS_SCORE`, `LLM_MAX_TOKENS`, plus the provider-side spend cap. A single
run makes a bounded number of calls by design; retries are the usual source of
cost spikes. Compare *tokens per completed task* (not per run) across prompt or
model changes.

---

## 4. Health & readiness signals

| Signal | Source | Use |
|---|---|---|
| `GET /api/health` | app router | `status`, `version`, `llm_provider`, `llm_model`, `database` |
| image `HEALTHCHECK` | `Dockerfile` | stdlib `urllib` request to `/api/health`; container becomes `unhealthy` after 3 failures |
| `pg_isready` | Compose postgres | gates backend start; also a DB availability signal |
| `docker compose ps` | Compose | `healthy`/`unhealthy`/`restarting` |
| `GET /api/metrics/runs` | app router | success rate, avg duration, tokens, retries |

The health payload intentionally reports the configured LLM provider/model and
a database status string so a single call distinguishes
"app up but DB down" from "LLM provider misconfigured".

---

## 5. Optional: LangSmith tracing

The `langsmith` package is already installed. Enable tracing via environment
(no code change required for LangChain/LangGraph instrumented paths):

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=agentforge
```

`app/core/config.py` reads `LANGSMITH_TRACING` / `LANGSMITH_API_KEY`; the
LangChain ecosystem also honours the legacy `LANGCHAIN_TRACING_V2`,
`LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT` names — prefer the `LANGSMITH_*` forms
for this project. Tracing is **off by default**; when off, no data leaves the
process. When on, prompts/completions go to the LangSmith SaaS — review the
data-processing implications before enabling it on real business data.

---

## 6. Optional: OpenTelemetry (scaffolding — not wired yet)

Status, stated honestly:

- `requirements.txt` includes **`opentelemetry-api` only** — no SDK, no OTLP
  exporter. The application today does **not** emit OTel traces/metrics/logs.
- Because no new Python packages are allowed, this repository does not add
  `opentelemetry-sdk` / `opentelemetry-exporter-otlp`.
- To use OTel, add those packages as a deliberate dependency change, initialise
  a `TracerProvider` with an OTLP exporter in `app/main.py`, and optionally
  instrument FastAPI/SQLAlchemy/httpx.

The infrastructure is ready for that step:

- Compose profile `observability` starts
  `otel/opentelemetry-collector-contrib` with an OTLP receiver on **4317**
  (gRPC) / **4318** (HTTP) and a `debug` exporter:
  `docker compose --profile observability up -d`.
- Collector config: `docker/otel-collector/config.yaml` (memory-limited batch
  pipeline; swap `debug` for `otlphttp`/`prometheus` to ship elsewhere).
- Backend env (already set by Compose, harmless when the app is uninstrumented):
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317`,
  `OTEL_SERVICE_NAME=agentforge-backend`.
- Verify the collector receives data:
  ```powershell
  docker compose --profile observability logs -f otel-collector
  ```

Do not claim traces exist in dashboards until an SDK/exporter is added. Until
then, use the JSON logs + `run_events` audit trail for internals.

---

## 7. Profiling and performance investigation

The orchestrator is I/O-bound (LLM + DB), so wall-clock per step and token
counts matter more than CPU. Practical tools:

**Per-step timing from the audit trail** (best first step):

```powershell
docker compose exec postgres psql -U agentforge -d agentforge -c `
  "SELECT type, agent, task_id, duration_ms FROM run_events WHERE run_id='<id>' AND duration_ms IS NOT NULL ORDER BY duration_ms DESC;"
```

**Python profiling (local):**

```powershell
# profile a short run: cProfile around the orchestrator entrypoint
venv\Scripts\python.exe -m cProfile -o profile.out -m uvicorn app.main:app --port 8000
# after exercising the run, inspect the top cumulative calls:
venv\Scripts\python.exe -c "import pstats; pstats.Stats('profile.out').sort_stats('cumulative').print_stats(30)"
```

```powershell
# import-time / startup cost
venv\Scripts\python.exe -X importtime -c "import app.main" 2> importtime.txt
```

**Container/DB level:**

```powershell
docker stats --no-stream
docker compose exec postgres psql -U agentforge -d agentforge -c `
  "SELECT query, calls, mean_exec_time FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;"
```
(`pg_stat_statements` requires enabling the extension in `postgresql.conf`; it is
not enabled by default.)

**External profilers** (`py-spy`, `scalene`) are not project dependencies; if
needed, install them in a separate tooling env (`pipx install py-spy`) and
attach to the process. Always profile with `LLM_PROVIDER=mock` for deterministic
comparisons, then validate once with the real provider.

**Known cost/latency hotspots:** LLM wall-clock (dominant), evaluator re-runs
after failed tasks, large RAG context injection, and serialised step execution.
Check `duration_ms` on `agent.completed`/`tool.called` events before optimising.

---

## 8. Alerting suggestions

There is no bundled alerting stack; wire these into your platform's monitoring:

| Condition | Suggested alert |
|---|---|
| `/api/health` non-200 for 2 min | page: app down |
| `docker inspect .State.Health.Status != healthy` | page: container unhealthy |
| postgres `pg_isready` failing | page: database down |
| runs with status `failed` in last 15 min > threshold | ticket: agent/tool regression |
| run duration > 2× rolling p95 | ticket: provider latency or retry storm |
| `token_usage.total_tokens` per day > budget | ticket: cost guard |
| runs stuck in `awaiting_approval` > 24 h | ticket: approval backlog |
| disk usage on `pgdata`/`artifacts` > 80 % | ticket: growth |

Because metrics are exposed over HTTP (`/api/metrics/runs`) rather than a
Prometheus endpoint, a simple cron/script probe or reverse-proxy log-based
metric is the lowest-effort integration today. Native Prometheus metrics would
be a deliberate dependency change (e.g. `prometheus-fastapi-instrumentator`),
which this project intentionally avoids.

---

## 9. Data retention and privacy

- `run_events`, `runs.state_json`, `artifacts` and `evaluation_metrics` may
  contain business data and LLM prompts/completions. Treat DB dumps and artifacts
  as sensitive.
- No automatic retention/TTL is implemented. Define a policy (e.g. delete runs
  older than N days) before production use; take a backup first.
- Deletion should cascade through `runs` (children have `ON DELETE CASCADE`) or
  go through the API; removing rows directly from `run_events` breaks the
  monotonic `seq` audit story for that run.
- LangSmith tracing, when enabled, is an external data flow — document it in your
  privacy review and restrict it to non-sensitive environments if needed.

---

## 10. Quick reference

```powershell
# live app logs (JSON) + errors only
docker compose logs -f backend
docker compose logs backend | Select-String '"level":"ERROR"'

# health + metrics
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/metrics/runs

# one run's audit trail
Invoke-RestMethod "http://127.0.0.1:8000/api/runs/<id>/events?after_seq=0"

# events straight from Postgres
docker compose exec postgres psql -U agentforge -d agentforge `
  -c "SELECT seq, type, agent, task_id, duration_ms FROM run_events WHERE run_id='<id>' ORDER BY seq;"

# optional collector
docker compose --profile observability up -d
docker compose --profile observability logs -f otel-collector
```
