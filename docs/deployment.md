# AgentForge — Deployment & Operations Guide

This guide covers the two supported ways to run AgentForge:

1. **Local development** — venv + uvicorn + Vite dev server (fast iteration).
2. **Production-style containers** — one application image plus Postgres 16 +
   pgvector, orchestrated by Docker Compose.

Contracts referenced here: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) and
[`docs/API_CONTRACT.md`](API_CONTRACT.md). Runtime settings are defined in
`app/core/config.py`; the template is [`.env.example`](../.env.example).

---

## 1. Topology

```
                         ┌──────────────────────────────────────────┐
   browser ── HTTP ────► │ backend container (FastAPI + uvicorn)    │
             /, /assets  │  • serves built SPA from frontend/dist   │
             /api/*      │  • REST + SSE (/api/runs/{id}/events/…)  │
                         │  • orchestrator runs in-process          │
                         │  • artifacts on named volume             │
                         └───────────────┬──────────────────────────┘
                                         │ SQLAlchemy (psycopg v3)
                         ┌───────────────▼──────────────────────────┐
                         │ postgres container (pgvector/pgvector:16)│
                         │  • tables created by SQLAlchemy create_all│
                         │  • named volume pgdata                   │
                         └──────────────────────────────────────────┘
   optional profiles:   redis (jobs)        otel-collector (observability)
```

One deployable application image contains the frontend build, the Python
backend, the demo sales database and the knowledge corpus.

---

## 2. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12.x | pinned dependency set in `requirements.txt` |
| Node.js | 24.x | frontend build only (`frontend/package.json`) |
| Docker Engine / Desktop | with Compose v2.24+ | Compose is needed for `env_file: required:` support |
| GNU Make (optional) | any | Windows: `winget install ezwinports.make`; ops can use raw commands below |

No Docker daemon is required to parse the compose file: `docker compose config`
is a client-side operation.

---

## 3. Local development (Windows PowerShell)

The commands below assume the repository root
(`C:\Users\Mustafa\AgentForge`); on bash/zsh replace paths with `venv/bin/...`.

### 3.1 One-time setup

```powershell
cd C:\Users\Mustafa\AgentForge

python -m venv venv
.\venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process Bypass

python -m pip install --upgrade pip
pip install -r requirements.txt

Copy-Item .env.example .env          # then edit .env (see §3.2)
```

Bash equivalent:

```bash
python3.12 -m venv venv && source venv/bin/activate
python -m pip install --upgrade pip && pip install -r requirements.txt
cp .env.example .env
```

### 3.2 Choose a configuration

Edit `.env` for one of these modes:

```dotenv
# (a) live provider
LLM_PROVIDER=groq
LLM_MODEL=openai/gpt-oss-20b
GROQ_API_KEY=gsk_...

# (b) offline / deterministic (tests, demos without keys, CI)
LLM_PROVIDER=mock

# Database: SQLite by default; set DATABASE_URL to a Postgres URL to use pgvector
DATABASE_URL=sqlite:///./data/agentforge.db
```

`SECRET_KEY` must be changed for anything shared:

```powershell
venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 3.3 Run the backend

```powershell
# from the repo root, with .env present
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

- API: <http://127.0.0.1:8000/api/...> (contract in `docs/API_CONTRACT.md`)
- Health: `Invoke-RestMethod http://127.0.0.1:8000/api/health`
- Interactive docs (FastAPI): <http://127.0.0.1:8000/docs>

SQLAlchemy creates missing tables on startup (`app/db/base.py:init_db`). To do
it explicitly:

```powershell
venv\Scripts\python.exe -c "from app.db.base import init_db; init_db()"
```

### 3.4 Run the frontend (hot reload)

```powershell
cd frontend
npm ci                # reproducible; falls back to `npm install` without a lockfile
npm run dev           # http://localhost:5173, proxies /api -> 127.0.0.1:8000
```

Production-like check (FastAPI serves the SPA from `frontend/dist`):

```powershell
cd frontend
npm run build         # tsc --noEmit && vite build -> frontend/dist
cd ..
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000/
```

### 3.5 Tests and demo

```powershell
# full test suite (offline)
$env:LLM_PROVIDER = "mock"
venv\Scripts\python.exe -m pytest -q

# real end-to-end demo (requires a live provider key in .env)
venv\Scripts\python.exe scripts\demo_run.py
```

### 3.6 GNU Make shortcuts

```powershell
make install      # venv + pip deps + frontend deps
make dev          # uvicorn --reload
make dev-frontend # Vite dev server
make build        # frontend production bundle
make test         # pytest -q
make lint         # byte-compile app/ (+ frontend lint script if configured)
make demo         # scripts/demo_run.py
make docker-up    # compose: Postgres + backend
make check        # lint + test + compose-config
```

Without GNU Make, every target maps to a single command documented above or in
§5/§6.

---

## 4. Environment matrix

All variables are optional; defaults live in `app/core/config.py`. Empty values
fall back to the defaults. `*` marks secrets — never commit them.

| Variable | Default | Purpose |
|---|---|---|
| `APP_NAME` | `AgentForge` | Display name |
| `APP_VERSION` | `1.0.0` | Reported by `/api/health` |
| `APP_ENV` (`ENVIRONMENT` legacy) | `development` | Environment label |
| `DEBUG` | `false` | Verbose app behaviour |
| `SECRET_KEY` * | dev placeholder | HS256 JWT signing key — **rotate in prod** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `720` | JWT lifetime |
| `DEMO_USER_EMAIL` | `demo@agentforge.io` | Seeded demo login |
| `DEMO_USER_PASSWORD` * | `demo1234` | Seeded demo password |
| `DEMO_USER_NAME` | `Demo Operator` | Seeded demo display name |
| `DATABASE_URL` | `sqlite:///<repo>/data/agentforge.db` | `sqlite…` or `postgresql+psycopg://…` |
| `SQLITE_ECHO` | `false` | Echo SQL for SQLite |
| `LLM_PROVIDER` | inferred / `groq` | `groq`, `openai`, `ollama`, `mock` |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Model id (`GROQ_MODEL` legacy fallback) |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `LLM_MAX_TOKENS` | `3000` | Per-call output cap |
| `LLM_TIMEOUT_SECONDS` | `90` | Per-call network timeout |
| `LLM_MAX_CALLS_PER_RUN` | `40` | Run-level budget guard |
| `GROQ_API_KEY` * | — | Groq credential |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Groq endpoint |
| `OPENAI_API_KEY` * | — | OpenAI credential |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI default model |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Local Ollama endpoint |
| `OLLAMA_MODEL` | `llama3.1` | Local Ollama model |
| `MAX_AGENT_RETRIES` | `2` | Evaluator-triggered retries per task |
| `EVAL_PASS_SCORE` | `0.72` | Pass threshold (0–1) |
| `STEP_TIMEOUT_SECONDS` | `240` | Per-step timeout |
| `HITL_ENABLED` | `true` | Approval gate for sensitive tools |
| `AUTO_APPROVE_DEFAULT` | `false` | Auto-approve (demo use only!) |
| `PLANNER_TEMPERATURE` | `0.1` | Planner determinism |
| `EMBEDDING_DIM` | `384` | Local embedding dimension |
| `RAG_TOP_K` | `5` | Retrieved chunks per query |
| `RAG_MIN_SCORE` | `0.08` | Retrieval floor |
| `RAG_CHUNK_SIZE` | `700` | Chunk size (chars) |
| `RAG_CHUNK_OVERLAP` | `100` | Chunk overlap |
| `SALES_DB_PATH` | `<repo>/data/demo/sales.db` | Demo SQLite sales DB |
| `KNOWLEDGE_DIR` | `<repo>/data/knowledge` | Knowledge corpus directory |
| `ARTIFACTS_DIR` | `<repo>/artifacts` | Report/artifact output |
| `FRONTEND_DIST_DIR` | `<repo>/frontend/dist` | Built SPA served at `/` |
| `CORS_ORIGINS` | localhost:5173/8000 | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | Root log level |
| `LOG_JSON` | `true` | JSON lines vs human-readable |
| `LANGSMITH_TRACING` | `false` | Enable LangSmith tracing |
| `LANGSMITH_API_KEY` * | — | LangSmith credential |
| `LANGSMITH_PROJECT` | `agentforge` | LangSmith project name |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` * | `agentforge` | Compose Postgres (compose-only) |
| `VITE_API_BASE_URL` | empty | Frontend build-time API base (empty = same-origin) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | OTel collector endpoint (profile) |
| `OTEL_SERVICE_NAME` | `agentforge-backend` | OTel service name |
| `OTEL_COLLECTOR_IMAGE` | `otel/opentelemetry-collector-contrib:0.104.0` | Collector image (profile) |

---

## 5. Docker: single container

Build the multi-stage image (Node 24 builds the SPA, then Python 3.12-slim runs
uvicorn as non-root uid 10001):

```powershell
docker build -t agentforge-backend:local .
docker image inspect agentforge-backend:local --format '{{.Config.User}}'  # appuser
```

Run it against SQLite (ephemeral; good for a quick demo). Note that `.env` is
**not** baked into the image:

```powershell
docker run --rm -p 8000:8000 --env-file .env `
  -e DATABASE_URL=sqlite:////app/data/agentforge.db `
  -e LLM_PROVIDER=mock `
  agentforge-backend:local
```

The image healthcheck probes `GET /api/health` from inside the container:

```powershell
docker inspect --format '{{.State.Health.Status}}' <container-id>
```

For durable data and Postgres, use Compose (§6) instead.

---

## 6. Docker Compose: production path

### 6.1 First start

```powershell
cd C:\Users\Mustafa\AgentForge
Copy-Item .env.example .env        # set real SECRET_KEY / GROQ_API_KEY (or LLM_PROVIDER=mock)

docker compose config              # client-side parse; does not need the daemon
docker compose build
docker compose up -d
docker compose ps                  # backend healthy + postgres healthy
```

What Compose creates:

| Service | Image | Notes |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | `pgdata` volume, `pg_isready` healthcheck, loopback port `127.0.0.1:5432` |
| `backend` | built from `Dockerfile` | `artifacts` volume, port `8000`, depends on healthy Postgres |
| `redis` (profile `jobs`) | `redis:7-alpine` | reserved for future background jobs — current code doesn't use it |
| `otel-collector` (profile `observability`) | otel contrib | OTLP receiver, debug exporter |

Container-internal `DATABASE_URL` is set by Compose to:

```text
postgresql+psycopg://agentforge:agentforge@postgres:5432/agentforge
```

If you change `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` in `.env`, update
the `DATABASE_URL` override in `docker-compose.yml` to match.

Enable optional profiles explicitly:

```powershell
docker compose --profile jobs up -d
docker compose --profile observability up -d
docker compose --profile jobs --profile observability up -d
```

### 6.2 Verify

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
# -> status, version, llm_provider, llm_model, database

docker compose logs -f backend
docker compose exec postgres pg_isready -U agentforge -d agentforge

# pgvector is available in the database (extension created by initdb):
docker compose exec postgres psql -U agentforge -d agentforge `
  -c "SELECT extname, extversion FROM pg_extension;"
```

### 6.3 Stop / restart / wipe

```powershell
docker compose down            # stop, keep volumes (data safe)
docker compose restart backend
docker compose down -v         # DANGER: deletes pgdata, artifacts and redisdata
```

### 6.4 Backups (PowerShell redirect caveat)

Named volumes are not host paths; dump via the container. In Windows PowerShell
5.1 `>` writes UTF-16, so pipe through `cmd` or use `-Encoding utf8`:

```powershell
cmd /c "docker compose exec -T postgres pg_dump -U agentforge -d agentforge > backup.sql"
# restore
cmd /c "docker compose exec -T postgres psql -U agentforge -d agentforge < backup.sql"
```

Artifacts (reports) live in the `agentforge_artifacts` volume:

```powershell
docker compose cp backend:/app/artifacts ./artifacts-backup
```

---

## 7. Database, pgvector and migrations

- **Local dev:** SQLite (`data/agentforge.db`), same SQLAlchemy models, WAL mode
  and foreign keys enabled (`app/db/base.py`).
- **Production:** PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16`). Compose
  passes `DATABASE_URL` with the `psycopg` (v3) driver.
- **Schema creation:** `Base.metadata.create_all` via `init_db()` on startup.
  There is **no Alembic** in this project and no new dependencies are added, so:
  - `create_all` creates missing tables/indexes but **never alters** existing ones.
  - For schema changes without a migration framework, the safe production
    procedure is: back up the volume → `docker compose down` → recreate the
    database/volume with the new model version → restore data if needed.
  - Plan a future Alembic adoption for zero-downtime upgrades.
- **pgvector provisioning:** `docker/initdb/01-init.sql` runs once on a fresh
  `pgdata` volume and runs `CREATE EXTENSION IF NOT EXISTS vector;` (plus
  `pg_trgm`). The image already contains the extension binaries.
- **Current vector storage:** `requirements.txt` does not include the `pgvector`
  Python package, so the app persists embeddings in
  `knowledge_chunks.embedding_json` (JSON list of floats) and ranks them with
  local cosine similarity — portable across SQLite and Postgres. The extension
  is provisioned so a native `vector(384)` column + HNSW index is a model-only
  change (see schema notes in `docker/initdb/01-init.sql`).
- **Reset the database:** `docker compose down -v` then `docker compose up -d`
  re-runs initdb and recreates tables on first backend start. This deletes runs,
  artifacts and knowledge.

---

## 8. Health and readiness

| Probe | Where | Expected |
|---|---|---|
| `GET /api/health` | app | `{status, version, llm_provider, llm_model, database}` |
| Docker `HEALTHCHECK` | image | Python stdlib request to `127.0.0.1:8000/api/health` every 30 s, 3 retries, 30 s start period |
| `pg_isready` | Compose postgres | exit 0, gates backend startup via `depends_on: service_healthy` |
| `docker compose ps` | Compose | backend `healthy`, postgres `healthy` |

Readiness recommendations:

```powershell
# container-level
docker inspect --format '{{json .State.Health}}' agentforge-backend

# API-level
Invoke-RestMethod http://127.0.0.1:8000/api/health | ConvertTo-Json

# database round-trip (reported in /api/health.database)
docker compose exec postgres psql -U agentforge -d agentforge -c "SELECT 1;"
```

Notes:

- The health endpoint is the contract path `/api/health`. If it 404s, confirm the
  API router is mounted under `/api` in `app/main.py`.
- `HEALTHCHECK` is intentionally stdlib-only (no `curl` in the image).
- For external load balancers, add a separate liveness probe on `/api/health`
  with a 2–5 s timeout and a 30 s failure threshold.

---

## 9. Security checklist

Before exposing an instance beyond localhost:

- [ ] Generate a fresh `SECRET_KEY` and set it via environment/secret store.
- [ ] Replace the demo credentials (`DEMO_USER_PASSWORD`) or disable the demo
      seed path in production.
- [ ] Never bake `.env` into images (already excluded by `.dockerignore`);
      inject secrets at runtime (Compose `env_file`, orchestrator secrets).
- [ ] Rotate `GROQ_API_KEY` / `OPENAI_API_KEY`; scope keys tightly and set
      provider-side spending limits.
- [ ] Restrict `CORS_ORIGINS` to the real dashboard origin(s); remove localhost
      entries in production.
- [ ] Keep `HITL_ENABLED=true` and `AUTO_APPROVE_DEFAULT=false`; sensitive tools
      must not run unattended.
- [ ] Bind Postgres to `127.0.0.1` only (as in `docker-compose.yml`) or remove
      its `ports:` mapping entirely; never publish 5432 publicly.
- [ ] Terminate TLS in front of the app (reverse proxy/ingress) and forward
      `X-Forwarded-*`; don't expose uvicorn directly to the internet.
- [ ] Run the container as non-root (uid 10001, already configured) and keep
      `no-new-privileges:true`; consider `read_only: true` + tmpfs for further
      hardening after testing app writes.
- [ ] Pin base images by digest for reproducible production builds; keep
      `requirements.txt` fully pinned (it is) and review Dependabot updates.
- [ ] Keep the database volume encrypted at rest (host/cloud disk encryption)
      and back it up per §6.4.
- [ ] Review `run_events`/`artifacts` for PII before sharing dumps; reports may
      contain business data.
- [ ] Add rate limiting/reverse-proxy auth if the dashboard is internet-facing
      (not implemented in-app).
- [ ] Set `LOG_JSON=true` in containers and redact secrets — app logs redact DB
      URLs (`_redact` in `app/db/base.py`); never log prompts containing secrets.

---

## 10. Operations, scaling and restarts

- **Single instance only (currently).** The event broker (`app/core/events.py`)
  is in-process, workflow runs execute as background tasks in the serving
  process, and the LangGraph `MemorySaver` checkpointer is in-memory. Therefore:
  - SSE clients must be served by the same process that runs the workflow.
  - Do **not** scale the `backend` service to multiple replicas behind a load
    balancer without first externalising the broker (e.g. Redis pub/sub — the
    `jobs` profile leaves a slot for it) and using a shared checkpointer.
  - A container restart loses in-flight runs. `runs.state_json` snapshots after
    every node allow inspection; runs that require approval can be resumed after
    restart via the orchestrator service's `resume_run`.
- **Logs:** `docker compose logs -f backend` (JSON lines; see
  `docs/observability.md`). The json-file driver is capped at 10 MB × 3 files.
- **Graceful shutdown:** uvicorn handles SIGTERM; `init: true` reaps zombies.
- **Time zone:** timestamps are stored/emitted in UTC (ISO-8601).
- **Disk:** watch the `pgdata` and `artifacts` volumes and `docker system df`.

---

## 11. Rollback and upgrades

1. **Tag before deploying:** the Compose image is `agentforge-backend:local`.
   Tag releases (`docker tag agentforge-backend:local agentforge-backend:1.2.0`)
   and record the git SHA; use immutable tags/digests in real deployments.
2. **Back up first:**
   ```powershell
   cmd /c "docker compose exec -T postgres pg_dump -U agentforge -d agentforge > pre-upgrade.sql"
   docker compose cp backend:/app/artifacts ./artifacts-pre-upgrade
   ```
3. **Upgrade:** `docker compose build --pull` → `docker compose up -d`.
4. **Rollback:**
   ```powershell
   # point the image tag back to the previous release
   docker compose down
   docker compose up -d          # with the previous image/tag and env
   # if the schema changed incompatibly:
   cmd /c "docker compose exec -T postgres psql -U agentforge -d agentforge < pre-upgrade.sql"
   ```
   `docker compose down` keeps named volumes; only `down -v` deletes data.
5. **Schema compatibility:** because there is no migration tool, only additive
   changes (`create_all` creating new tables) are safe to roll forward/back.
   Destructive column changes require the backup/restore path above.

---

## 12. Cost notes

Cost is dominated by LLM tokens, not infrastructure.

- **Token accounting:** every LLM call returns `TokenUsage`
  (`prompt_tokens`, `completion_tokens`, `total_tokens`) and runs accumulate it
  in `runs.token_usage_json`, exposed as `token_usage` on `RunRead` and
  aggregated by `GET /api/metrics/runs`. Apply the provider's current per-token
  prices (prompt vs completion) to those counters; don't hardcode prices in code.
- **Budget guards already present:** `LLM_MAX_CALLS_PER_RUN` (default 40),
  `MAX_AGENT_RETRIES` (default 2), `EVAL_PASS_SCORE` (avoids pointless
  rework), `LLM_MAX_TOKENS`, and the deterministic `mock` provider for
  tests/demos/CI.
- **Demo:** set `LLM_PROVIDER=mock` (zero token cost) for screenshots, CI and
  smoke tests; use a real provider only for the final demo run.
- **Provider choice:** Groq is typically the cheapest/fastest hosted option for
  this workload; Ollama is free but needs a GPU/CPU host. Use a provider-side
  spend cap as the hard backstop.
- **Infrastructure:** a single small VM (2 vCPU / 2–4 GB RAM) runs Postgres +
  backend comfortably for demo traffic. Compose has no external managed services
  by default; optional LangSmith tracing adds no infra, just an external SaaS
  dependency. The optional otel-collector is lightweight (memory-limited to
  256 MiB in `docker/otel-collector/config.yaml`).
- **Storage:** SQLite/Postgres state is small (KBs–MBs per run); artifacts are
  Markdown and dominate volume growth.

---

## 13. CI/CD

`.github/workflows/ci.yml` runs on push/PR:

1. **backend** — Python 3.12, `pip install -r requirements.txt`, `pytest -q`
   with `LLM_PROVIDER=mock` and SQLite.
2. **frontend** — Node 24, `npm ci`/`npm install`, `npm run build`, uploads
   `frontend/dist`.
3. **docker** — validates `docker compose config`, builds the image (no push)
   and asserts it runs as `appuser` with port `8000` exposed.

Images are **not** pushed by CI. To publish, add a registry login step and
`docker build --push`/`docker/build-push-action` after the `docker` job.

---

## 14. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `docker compose` blocked / cannot connect to daemon | Docker Desktop isn't running. Start it, then rerun. `docker compose config` still works without the daemon. |
| `env file .env not found` | Copy `.env.example` to `.env`, or rely on the optional `env_file` (Compose ≥2.24). |
| backend unhealthy, `/api/health` 404 | API router not mounted under `/api`; check `app/main.py`. Until the API layer is wired, the bootstrap app only exposes `/health`. |
| backend unhealthy, `/api/health` times out | uvicorn never finished startup (bad `DATABASE_URL`, missing demo data, import error) — read `docker compose logs backend`. |
| backend restarts immediately | `DATABASE_URL` credentials don't match the postgres service; fix both or reset `.env`. |
| `relation "…" does not exist` | `init_db()` didn't run on startup, or an older DB volume predates a model. Run `make db-init` / the init command in §3.3, or recreate the volume. |
| pgvector `extension "vector" is not available` | You're not using the `pgvector/pgvector:pg16` image, or initdb didn't run on an existing volume. Recreate with `docker compose down -v`. |
| port 8000/5432 already in use | Stop the conflicting process/container or change the host side of the `ports:` mapping. |
| frontend 404 at `/` but API works | `frontend/dist` was not built/copied; rebuild the image (multi-stage) or run `npm run build` locally. |
| CORS errors in browser dev | Add `http://localhost:5173` to `CORS_ORIGINS`, or use the Vite proxy. |
| Windows line endings in scripts | Not an issue for the image (Linux stage). For local bash scripts, use `.gitattributes`/LF or run via WSL. |
| `make` not recognized on Windows | Install GNU Make, or use the raw commands in this guide. |
| Slow first Docker build | Multi-stage build downloads Node and Python base images and installs both dependency trees; subsequent builds hit the layer cache. |

---

## 15. Quick command reference

```powershell
# local
python -m venv venv; .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
venv\Scripts\python.exe -m uvicorn app.main:app --reload
cd frontend; npm ci; npm run dev

# tests / demo
$env:LLM_PROVIDER="mock"; venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe scripts\demo_run.py

# docker
docker build -t agentforge-backend:local .
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs -f backend
docker compose down            # keep data
docker compose down -v         # delete data

# health
Invoke-RestMethod http://127.0.0.1:8000/api/health
docker compose exec postgres pg_isready -U agentforge -d agentforge
```
