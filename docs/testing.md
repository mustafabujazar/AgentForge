# AgentForge Testing & QA Strategy

This document describes how AgentForge is tested, how the suite stays fully
offline and deterministic, what is intentionally **not** covered, and how to
reproduce every quality gate.

## 1. Test pyramid and suites

| Layer | File | What is real | What is faked | Tests |
|---|---|---|---|---|
| Unit / foundation | `tests/test_foundation.py` | security, schemas, registry, broker, LLM layer | nothing (pure in-process) | 134 |
| API integration | `tests/test_integration_api.py` | FastAPI app, routers, JWT, SQLAlchemy/SQLite, RAG store | orchestrator (`workflow_service` stub) | 73 |
| End-to-end workflow | `tests/test_e2e_workflow.py` | `WorkflowService`, LangGraph graph, 4 agents, tools, evaluator/retry, HITL gate, synthesizer, DB, events | only the LLM provider (scripted) | 6 |
| Benchmark | `scripts/benchmark.py` → `docs/BENCHMARK.md` | full workflow over 4 business objectives | LLM provider in the default offline mode | 4 scenarios |

The full suite (`pytest -q` from the repository root) currently runs **257 tests
in ~40-50s** on the reference Windows/Python 3.12 environment. Every test is
offline: outbound TCP is blocked session-wide and no uvicorn/Docker process is
started.

## 2. Running the suite

```powershell
# whole suite (use the venv interpreter; no extra packages are installed)
.\venv\Scripts\python.exe -m pytest -q

# one layer
.\venv\Scripts\python.exe -m pytest tests/test_foundation.py -q
.\venv\Scripts\python.exe -m pytest tests/test_integration_api.py -q
.\venv\Scripts\python.exe -m pytest tests/test_e2e_workflow.py -q

# filter by marker
.\venv\Scripts\python.exe -m pytest -q -m integration
.\venv\Scripts\python.exe -m pytest -q -m e2e
.\venv\Scripts\python.exe -m pytest -q -m "not slow"
```

Notes:

* Run from the repository root. `tests/conftest.py` adds the root to
  `sys.path`, so `app.*` imports work with either `python -m pytest` or the
  `pytest` console script.
* **pytest-asyncio is not installed.** Async behaviour is tested with
  `asyncio.run(...)` inside normal sync tests (event broker) and through
  `fastapi.testclient.TestClient` (API/SSE). Do not add `async def` tests.
* No pip installs are required or permitted. Everything uses the pinned venv
  dependency set (`pytest`, `httpx`, `fastapi`, `sqlalchemy`, `langgraph`, …).
* Markers are registered in `conftest.py`: `offline`, `integration`, `e2e`,
  `slow`, `real_llm` (never enabled in CI).

## 3. Test data and isolation

The suite must never touch `data/agentforge.db` (the developer database) and
must not leak state between tests. The isolation model is:

1. **Environment before imports.** `tests/conftest.py` sets
   `DATABASE_URL`, `LLM_PROVIDER=mock`, empty provider keys, `APP_ENV=test`,
   `ARTIFACTS_DIR`, `KNOWLEDGE_DIR`, `SALES_DB_PATH`, deterministic thresholds
   and logging levels at **module import time — before any `app.*` module is
   imported by a test module**. This matters because `app.db.base` builds the
   SQLAlchemy engine at import (`SessionLocal = get_session_factory()`), so a
   later `configure_database()` would leave the `SessionLocal` alias pointing
   at a disposed engine. The guard fixture asserts the app under test is bound
   to a `sqlite:///` URL inside the session temp directory and that
   `LLM_PROVIDER=mock` / keys are blank.
2. **One throwaway SQLite file per pytest session** under
   `%TEMP%/agentforge-tests-*`, with per-test schema reset: the autouse
   `fresh_db` fixture disposes pooled connections, runs
   `Base.metadata.drop_all()` + `create_all()` and clears process-wide
   singletons (event broker subscribers + bound loop, LLM provider cache, RAG
   knowledge store, API↔RAG bridge cache).
3. **Temp artifacts/knowledge directories.** `ARTIFACTS_DIR` points inside the
   session temp dir, so agent reports never pollute the repo. `KNOWLEDGE_DIR`
   is an **empty** temp directory so API startup seeding is a no-op and fast;
   e2e tests ingest two small memos explicitly when they need RAG hits (the
   real 92-chunk corpus is used by the benchmark instead).
4. **Lifespan is exercised.** The `client` fixture uses
   `with TestClient(app) as client`, so schema bootstrap, demo-user seeding and
   SSE broker loop binding run exactly like a served app.
5. **Network is blocked.** A session-wide socket guard raises on any
   non-loopback `connect()`/`create_connection()` (loopback is allowed because
   Windows' asyncio Proactor event loop builds its self-pipe with
   `socket.socketpair()`). Combined with `LLM_PROVIDER=mock` and blank API keys,
   a misconfigured test cannot reach Groq/OpenAI/Ollama.
6. **Background workflow threads.** Real runs are either synchronous
   (`run_sync`) or waited on through the DB (`wait_for`, up to timeout) and
   joined before teardown so a worker cannot write into the next test's fresh
   schema.

The developer database, `data/demo/sales.db` and `data/knowledge/` are read-only
inputs; tests that need the demo sales data use the real SQLite file through the
`sales_sql` tool (read-only `mode=ro` connection).

## 4. Fixtures reference (`tests/conftest.py`)

| Fixture | Scope | Purpose |
|---|---|---|
| `fresh_db` | function, autouse | Drop/create schema + reset singletons per test |
| `settings` | function | Cached `Settings` bound to the test environment |
| `db_session` | function | Session against the isolated test DB |
| `client` | function | `TestClient(app)` with lifespan |
| `demo_token` | function | Logs in as the seeded demo user (`demo@agentforge.io`), registering it if seeding is absent |
| `auth_headers` | function | `{"Authorization": "Bearer <token>"}` |
| `demo_user` | function | `GET /api/auth/me` payload for the demo user |
| `fake_llm` / `scripted_llm` | function | Deterministic `BaseLLM` installed app-wide (see §5) |
| `no_network` | function | Extra guard that fails if `OpenAICompatibleLLM.invoke` is called |
| `wait_for` | function | Poll a predicate with timeout (background runs/statuses) |

Helpers exported from `tests/test_integration_api.py`: `StubWorkflowService`
(async `start_run`/`resume_run`, sync `cancel`) is injected through the
documented seam `app.api.runs.workflow_service`. The e2e file exports
`WorkflowResponder`, a role-aware scripted LLM.

## 5. LLM mocking

No test may call a real model. Two layers enforce this:

1. **Environment:** `LLM_PROVIDER=mock`, `GROQ_API_KEY=""`, `OPENAI_API_KEY=""`,
   `OLLAMA_BASE_URL=""` set before imports; the offline guard fixture asserts
   them. `app.llm.get_llm()` therefore resolves to `MockLLM` in foundation
   tests.
2. **Injection:** the `fake_llm` / `scripted_llm` fixtures patch
   `app.llm.registry._build_llm` (so the *cached real factory* returns the fake),
   `app.llm.registry.get_llm`, `app.llm.get_llm`, and scan `sys.modules` for any
   `app.*` module that imported `get_llm` directly. This covers every import
   style the orchestrator/agents may use without editing their code.

`ScriptedLLM` supports a response queue (`responses=[...]`) or a responder
callable `responder(messages, json_mode) -> str` and records `calls`, token
counts and prompts. The e2e `WorkflowResponder` detects the role from the system
prompt (`PLANNER`, `EVALUATOR`, `SYNTHESIZER`, `RESEARCH`, `ANALYST`,
`EXECUTION`, `REPORTING`) and scripts a realistic ReAct loop: one tool action
followed by a final JSON result, a plan with 4 dependent tasks, a passing LLM
judge, and a full Markdown report. `force_retry_role="analyst"` makes the first
analyst attempt deliberately weak so the deterministic evaluator rules fail and
the retry path is exercised. Note: the evaluator intentionally skips the LLM
judge when the deterministic rules already pass clearly (call-budget
optimisation), so the judge is only guaranteed to appear in the retry path
(`evaluator = "llm+rules"` vs `"rules"` is asserted/observed accordingly).

There is no live-provider test in the suite by design. The real Groq path is
validated manually with `python scripts/benchmark.py --provider groq` (see
`docs/BENCHMARK.md`); results are clearly marked as real and are never invented.

## 6. What each suite covers

**Foundation (unit, no DB/HTTP dependency):**
scrypt hash/verify (salt uniqueness, malformed hashes, empty password);
HS256 JWT round-trip, expiry, tampered signature/payload, malformed tokens;
`extract_json` (plain/fenced/preamble/trailing commas/braces in strings/single
quotes/unbalanced/garbage); `TaskSpec`/`Plan` validation incl. all agent
aliases, id normalization, duplicate-id dedupe, task-count/priority bounds;
`AgentRole`/`RunStatus`/`TaskStatus`/`TokenUsage`; tool registry registration,
duplicate handling, unknown tool, required/type validation, scope denial,
sensitive-tool approval gate, exception-to-failure conversion, `tool_names`,
`to_dicts`; event broker async pub/sub, multi-subscriber fan-out, subscriber
cleanup on `aclose`, thread-safe publish from a worker thread, `clear()`;
settings defaults/env parsing/DB-URL normalization; LLM registry (mock caching,
missing-key configuration errors, `provider_info`), `MockLLM` behaviour,
OpenAI-compatible payload/headers (no network); JSON/human log formatters and
the documented `extra` reserved-key caveat.

**API integration (real routers, stubbed orchestrator):**
health/`/api` root/SPA root; OpenAPI contains every path+method from
`docs/API_CONTRACT.md`; register/login/me incl. 409 duplicate, 422 validation,
401 wrong password/missing/garbage/expired/deleted-user token; run create
(202 + stub scheduled), list pagination/order/status filter, detail with plan and
token usage, tasks, report, artifact list/download (stored content, on-disk
file, ownership, 401/404), events + `after_seq`, SSE `text/event-stream` replay
for a terminal run + `after_seq`, 401/404; cancel (idempotent for terminal runs);
approvals valid approve/reject, 404 unknown/foreign approval, 409 already
decided/finished run, 422 invalid decision, resume-failure marks run failed;
metrics exact aggregation (success rate, tokens, retries, avg score, avg
duration, `by_status`) and member isolation; `/api/agents` and `/api/tools`
catalog shape with all five registered tools; knowledge ingest→search and
validation; 401 matrix for every protected endpoint; cross-tenant 404 matrix and
admin read-all behaviour.

**End-to-end (real workflow, fake LLM):**
analysis objective completes with a 4-task plan, all task records completed and
evaluated ≥ `EVAL_PASS_SCORE`, real `sales_sql`/`knowledge_search`/`report_writer`
tool calls, agent + final report artifacts persisted as `artifacts` rows and
files on disk, `runs.final_report` matching an artifact, contiguous durable
event log (`run.created` … `run.completed`) with tool/eval events, token usage,
`state_json` snapshot; retry path (weak first analyst attempt → `task.retry`,
attempts=2, then pass); auto-approve on a sensitive objective creates no
approval; HITL pause through the API (`awaiting_approval` + pending `Approval` +
`approval.required`/`run.awaiting_approval`), approve → completed + `approval.resolved`
+ downloadable artifact + `approval_granted` in metrics, reject → execution and
dependent reporting tasks skipped while the run still completes with an honest
report.

## 7. Known gaps and limitations

* **No live-model coverage in the automated suite.** By design (offline,
  deterministic, no secrets in CI). The real Groq path is a manual benchmark
  run; `tests` can never hit a provider.
* **SSE live tail mid-run is not asserted.** Integration tests cover auth
  (401), unknown run (404) and durable replay for terminal runs (with
  `after_seq`) to avoid a hanging stream in CI. The live-broker branch of
  `_event_stream` is therefore exercised only indirectly.
* **Checkpoint-loss resume fallback is not exercised.**
  `WorkflowService._fallback_resume_state` (process-restart recovery when the
  in-memory `MemorySaver` checkpoint is gone) is unit-covered by the
  orchestrator owner's tests, not by e2e.
* **No 403 surface in API v1.** `get_current_admin` exists, but no admin-only
  route is exposed, so cross-tenant access is asserted as **404 by design**
  (run ids are not probeable). 403 is recorded as N/A, not untested silently.
* **Approval decision attribution** is tested for the authenticated user and
  cross-tenant 404; "admin overrides someone else's approval" is not part of the
  v1 contract.
* **Benchmark numbers are orchestration metrics in offline mode**, not model
  quality or real-model latency; p50/p95 over n=4 scenarios are indicative only.
* **No coverage measurement** (`pytest-cov` is not installed, and no new
  packages are allowed); coverage is evidenced by suite structure instead.
* E2E requires the demo assets `data/demo/sales.db` and `data/knowledge/`; if
  they are missing the failures are explicit ("sales database not found…").
* The suite is Windows-oriented (socket-guard loopback allowance for the
  Proactor self-pipe) but otherwise portable; run it with the project venv.

## 8. QA issue ledger

Issues found by QA and their resolution status:

| Issue | Found by | Status |
|---|---|---|
| Agent-produced `report_writer` artifacts existed only as files + refs in `task_records.result_json`, never as `artifacts` rows (API artifact list omitted them) | e2e artifact assertions | **Fixed by orchestrator** — `Executor._persist_artifacts` + `persistence.ensure_artifact` (deduped by run+path); now asserted by `test_full_workflow_offline` and `test_agent_artifacts_are_persisted_as_rows` |
| Sub-second JWT expiry boundary test was flaky (`expires_minutes=-0.01`) | Lead/full-suite run | **Fixed in test** — 30s-in-the-past token (`expires_minutes=-0.5`), clock-independent; expiry rejection semantics unchanged |
| No 403 route exists despite `get_current_admin` | integration planning | Documented as intentional 404-to-avoid-probing; not a defect in v1 |

No unresolved external failures were known when this document was last updated;
the authoritative gate is `.\venv\Scripts\python.exe -m pytest -q`.

## 9. Benchmark

`scripts/benchmark.py` drives the real workflow over four business objectives
(sales analysis, revenue root-cause diagnosis, remediation plan behind an
approval gate, and a pricing-campaign action) and writes `docs/BENCHMARK.md`
plus a JSON summary. It uses an isolated temp SQLite DB and artifacts dir,
ingests `data/knowledge`, and records runs completed/total, success rate, wall
latency p50/p95, average evaluation score, retry count, tokens, artifacts and an
optional cost estimate:

```powershell
.\venv\Scripts\python.exe scripts/benchmark.py                     # offline scripted, deterministic
.\venv\Scripts\python.exe scripts/benchmark.py --provider groq --limit 4   # real API run, clearly marked
```

Offline results currently documented (last run): **4/4 completed, success rate
1.00, wall latency p50 ~0.41s / p95 ~0.51s, average evaluation 1.000 (the
deterministic-rules path is taken when the scripted results pass clearly),
0 retries, 29,714 scripted tokens, 2/2 approval-gated runs paused and resumed**.
These are orchestration-health numbers, not a model-quality claim.
