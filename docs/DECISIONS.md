# Architecture Decision Records

Short, honest records of the significant choices behind AgentForge and the tradeoffs
accepted for a portfolio build that must run locally on modest hardware.

---

## ADR-001 — LangGraph as the workflow controller

**Context.** Multi-agent demos often degenerate into free-form chat between agents.
That is hard to debug, evaluate and govern.

**Decision.** LangGraph owns control flow: a typed `StateGraph` with explicit nodes
(`plan → dispatch → execute → evaluate → retry → approval → synthesize`) and
conditional edges. Agents never talk to each other directly; they receive a defined
task plus shared state and return a structured `AgentResult`.

**Consequences.** Deterministic routing, inspectable state, retries and human approval
are first-class. The graph is coupled to LangGraph's state/checkpointer model, which
is a deliberate dependency for a portfolio targeting agent engineering roles.

---

## ADR-002 — Provider abstraction over a single SDK

**Context.** Hosted models are fast but cost money and may be unavailable; local
Ollama models are private but weak on a laptop GPU.

**Decision.** `app/llm` defines a small `BaseLLM` interface (`invoke(messages,
temperature, max_tokens, json_mode) -> LLMResponse`) with OpenAI-compatible
implementations for Groq, OpenAI and Ollama, plus a deterministic mock. Selection is
environment-driven (`LLM_PROVIDER`, `LLM_MODEL`). No agent code imports a vendor SDK.

**Consequences.** Swapping providers is a config change; tests/offline dev use the
mock. We implement retries/timeouts/JSON-mode ourselves, so we own those semantics.
Groq (`openai/gpt-oss-20b`) is the default hosted model for the demo because it is
fast and cheap.

---

## ADR-003 — SQLite locally, PostgreSQL + pgvector in production

**Context.** The target laptop has no PostgreSQL service and the Docker daemon is not
always available; a reviewer should be able to run the project with one command.

**Decision.** SQLAlchemy models are portable. Default local `DATABASE_URL` is SQLite
(WAL, foreign keys, busy timeout); Docker Compose runs PostgreSQL 16 with pgvector.
Placeholder credentials trigger a safe SQLite fallback with a warning instead of a
hang. Production RAG is designed for pgvector but the local store uses persisted JSON
vectors and pure-Python cosine similarity so no extension is required.

**Consequences.** Zero-setup demo and a credible production path. SQLite's single-writer
model means background workflow threads must keep transactions short — the code uses
short `session_scope()` blocks and monotonic per-run event sequencing rather than long
transactions.

---

## ADR-004 — Tools are typed, scoped capabilities, not arbitrary code

**Context.** "Give the agent tools" is where most agent projects become unsafe or
unbounded.

**Decision.** Every capability is a `BaseTool` with a `ToolSpec` (JSON input schema,
scopes, sensitivity, timeout). Agents can only call tools on their allowlist with
granted scopes. `sales_sql` is read-only with statement validation and row limits;
`sensitive=True` tools require a human approval flag in the tool context. The registry
validates required arguments and types before execution and never lets a tool
exception crash the workflow.

**Consequences.** Auditable tool calls (`ToolCallRecord`), bounded blast radius,
straightforward extension. The tradeoff is less free-form tool use than a fully
generic code interpreter.

---

## ADR-005 — Bounded ReAct loop per agent, JSON contracts at every boundary

**Decision.** Each specialist agent runs at most 3 tool steps. Every LLM step must
return a JSON object: either a tool call (`action=tool`) or a final structured result
(`action=final`). Planner, evaluator and synthesizer have typed Pydantic schemas.
Malformed JSON gets one repair attempt, then deterministic fallbacks.

**Consequences.** Token usage is bounded and outputs are validatable. The price is
prompt discipline and some verbosity; small models must follow the format or the
repair/fallback path handles it.

---

## ADR-006 — Evaluation combines rules and an LLM judge

**Decision.** Deterministic checks (result exists, evidence/findings present, tools
used when required, artifact for reporting tasks) are combined with an LLM judge that
returns a score and actionable feedback. Tasks below `EVAL_PASS_SCORE` retry up to
`MAX_AGENT_RETRIES` with feedback injected into the next attempt.

**Consequences.** Catches structurally empty or unsupported results; the judge adds
qualitative judgment. LLM judges can be inconsistent, so scores/feedback are persisted
and surfaced rather than hidden.

---

## ADR-007 — Human-in-the-loop only for sensitive actions

**Decision.** Planning marks action-taking tasks (`sensitive=True`) when the objective
asks the system to execute real-world changes. The workflow pauses before such a task,
persists an `Approval` row, and resumes only after an explicit approve/reject decision.
Analysis/report-only requests run end-to-end without interruption.

**Consequences.** Demonstrates governance without making every demo require a click.
Approval state is inspectable in the UI and API.

---

## ADR-008 — Observability is a first-class domain model, not logs

**Decision.** `run_events` is an append-only, per-run sequenced audit log in the
database; live SSE tails it through an in-process broker. Plan, task records,
evaluations, tool calls, token usage, artifacts and final reports are persisted.

**Consequences.** Every run is replayable and inspectable after the fact, and the UI
does not depend on log scraping. LangSmith/OpenTelemetry are optional add-ons, not
requirements.

---

## ADR-009 — React + Vite dashboard served by FastAPI

**Context.** The API needs a real UI, but a second container/domain is unnecessary for
this scope, and the sandbox blocks some native build tooling.

**Decision.** A React 18 + TypeScript SPA is built to `frontend/dist` and served by
FastAPI (single artifact for deployment). The UI uses SSE with polling fallback for
live updates. Where the sandbox blocks esbuild's child process, the build can fall
back to a `tsc`-only pipeline while keeping the same source and deployment layout.

**Consequences.** One deployable container, no CORS in production, simple local demo.
The tradeoff is no Next.js server features (SSR) — not needed for an authenticated
internal operations dashboard.

---

## ADR-010 — Security pragmatism on the pinned dependency set

**Decision.** Passwords use stdlib `hashlib.scrypt` with per-user salt; JWTs are
HMAC-SHA256 tokens built with `hmac`/`hashlib`; secrets come from environment
variables. No passlib/PyJWT dependency because the environment cannot install new
packages.

**Consequences.** No vendor lock and fewer dependencies; the implementation is small
and test-covered. Production hardening (key rotation, refresh tokens, RBAC expansion,
rate limiting) is documented in `docs/deployment.md`.
