# AgentForge — Mission Control (frontend)

A polished, dense **React 18 + TypeScript** single-page dashboard for the AgentForge
autonomous multi-agent platform. It is built to static assets in `frontend/dist/` and
served by FastAPI at `/`.

The aesthetic is deliberate mission control: near-black layered panels, a faint
engineering grid, monospace telemetry, teal/blue accents, status pulses and dense
information hierarchy — not a generic admin template. No UI kit, no Tailwind, no router
library, no Markdown library.

## Pages

| Route (hash) | What it does |
|---|---|
| `#/` | **Dashboard** — objective composer with 3 scenario presets, aggregate KPI strip, recent runs (filters, live refresh, pagination), agent roster, execution-loop explainer. |
| `#/runs/:id` | **Run detail** — status header + objective, approval banner (approve/reject + audit note), live SSE activity timeline with polling fallback, plan/task board with per-task agent, attempts, evaluation score and issues, SVG workflow graph, Markdown report, token accounting, artifacts with authenticated downloads. |
| `#/agents` | Agent capability registry (capabilities + bound tools). |
| `#/tools` | Tool catalog with categories, scopes, sensitive/HITL flags and JSON input schemas. |
| `#/knowledge` | Debounced search over the RAG retrieval store with similarity scores. |

Login pre-fills the seeded demo credentials (`demo@agentforge.io` / `demo1234`), stores the
JWT in `localStorage`, and any `401` from the API drops the session back to the login screen
with an explanatory notice.

## API integration (authoritative: `docs/API_CONTRACT.md`)

All calls go through one fetch wrapper (`src/api/client.ts`) that:

- prefixes `/api`, attaches `Authorization: Bearer <JWT>`;
- normalizes FastAPI `{"detail": …}` errors (including validation arrays) into `ApiError`;
- converts network failures into a readable "backend unreachable" error;
- centrally handles `401` → session clear + login redirect.

Notable contract details the dashboard depends on:

- **Live events are SSE over `fetch`, not `EventSource`.** EventSource cannot send an
  Authorization header, so `src/hooks/useLiveEvents.ts` reads the stream body via
  `ReadableStream`, parses frames with a small hand-written SSE parser
  (`src/lib/sse.ts`, tolerant of chunk splits and `: ping` heartbeats), dedupes by `seq`,
  and reconnects with `after_seq`. If the stream fails, ends early, or is unsupported it
  falls back to polling `GET /events?after_seq` every **1.5 s**, retrying SSE every ~9 s.
  The transport state is rendered as `Live` / `Polling 1.5s` / `Offline` / `Final`.
- **Metrics** come from `GET /metrics/runs` (`{total_runs, completed, failed, success_rate,
  avg_duration_seconds, total_tokens, total_retries, avg_eval_score, by_status}`). A `404`
  or outage degrades gracefully: KPIs are derived from the loaded runs and labelled as such.
- **Artifacts** come from `GET /runs/{id}/artifacts` with fallback to the report payload /
  `run.artifacts`. Downloads use `GET /artifacts/{id}/download` via authenticated
  `fetch` + `blob` (an `<a download>` cannot carry the bearer token) and are only offered
  for `kind=markdown|report`.
- **Report** is fetched from `GET /runs/{id}/report`; a `404` while the run is still active
  is treated as "not ready yet".
- Run, task, event and approval payload shapes tolerate optional/unexpected fields
  (`src/api/types.ts`) while matching the documented fields exactly.

Hash routing is intentional: deep links keep working when FastAPI serves only
`index.html` at `/` and static assets, with no SPA catch-all route required.

## Build

```powershell
$env:npm_config_cache="C:\Users\Mustafa\AgentForge\.npm-cache"
npm install --ignore-scripts
npm run build
npm run verify
```

Output:

```
dist/
  index.html
  assets/
    styles.css
    modules/          # tsc-emitted ES modules (31 files)
    vendor/           # React 18 UMD builds + ESM facades
```

### Why the build is bundler-free (important)

Vite has a hard dependency on **esbuild**, which runs as a native child process and talks
over anonymous pipes. The DSH Windows sandbox denies those pipes, so `vite build` fails
with:

```
Error: spawn EPERM   (esbuild ... ensureServiceIsRunning)
```

This is an environment limitation, not an application bug. `npm run build` therefore uses a
deterministic, no-child-process pipeline:

1. `tsc -p tsconfig.build.json` emits native ES modules to `dist/assets/modules`.
2. `node scripts/build-fallback.mjs` (pure Node `fs`, no child processes):
   - copies the React 18 UMD builds and writes tiny ESM facades
     (`window.React` / `window.ReactDOM`);
   - rewrites tsc's extensionless relative imports to `.js` and redirects the bare
     `react` / `react-dom/client` imports to the vendor facades;
   - removes the CSS side-effect import, copies `styles.css`, and writes `index.html`
     (React + ReactDOM UMD first, then the module entry).

The source stays standard Vite-compatible (`vite.config.ts`, dev proxy `/api` →
`http://127.0.0.1:8000`, `base: '/'`); `npm run build:vite` is available for environments
where esbuild is permitted. Because of the same sandbox restriction, **Vite's dev server
cannot run here** either — use the preview mock instead:

```powershell
# serves the built dist/ + a contract-shaped mock API (dev fixtures only)
python scripts/mock_api.py --port 8123
# open http://127.0.0.1:8123/__preview.html?to=/runs/demo-completed
```

`scripts/mock_api.py` is **not** production code; it exists only for offline visual review
(and it implements SSE, approvals, artifacts and metrics so every UI state can be checked).

## Verification

- `npm run typecheck` — strict TypeScript (no `any` escapes for API data; unknown payloads
  are narrowed defensively).
- `npm run build` — must exit 0.
- `npm run verify` — checks that every emitted import resolves, no bare/extensionless
  specifiers remain, index.html loads React UMD before the app, and every facade export
  exists on the React 18 UMD build. Also evaluates both UMD bundles in a Node VM.
- `npm run verify:ssr` — compiles a CommonJS smoke entry and renders **15 real component
  trees** through `react-dom/server` (login, shell+dashboard, all secondary pages, task
  board, timeline, graph, approval banner, report/markdown, artifacts, token usage) and
  asserts key markup, catching render-time errors without a browser.

## Structure

```
src/
  api/        types.ts, client.ts            # contract types + fetch wrapper
  components/ Shell, Icon, ui primitives, Markdown, Timeline, TaskBoard,
              WorkflowGraph, ApprovalBanner, TokenUsage, Artifacts, ReportPanel
  hooks/      useApi (async/polling/now), useRunDetail, useLiveEvents
  lib/        session, router, format, markdown parser, sse parser,
              runData (plan+task+event merge), download
  pages/      Login, Dashboard, Run, Agents, Tools, Knowledge
  styles.css  design tokens + full responsive system (900px+)
  App.tsx, main.tsx
scripts/      build-fallback.mjs, verify-build.mjs, ssr-smoke.tsx,
              mock_api.py (dev-only), run-ssr-smoke.mjs
```

## Known limitations / next steps

- **Browser screenshots could not be produced in this sandbox** (Chrome/Edge both fail to
  launch because mojo named pipes are denied). Rendering is covered by SSR smoke tests
  instead; a real-browser pass against the live API is still worth doing.
- The production build ships readable unminified modules rather than hashed/minified
  bundles (a consequence of the no-esbuild sandbox). Once build tooling is available,
  switching back to `vite build` would minify, hash and split chunks.
- Timeline keeps all events in memory; for very long runs (>10k events) a virtualized list
  or windowed buffer would be the next step.
- The workflow graph handles the plan's DAG shape but not manually reordered nodes; a
  drag-to-layout view could be added if plans grow beyond ~10 tasks.
- With more time: Playwright e2e tests against the FastAPI service, keyboard shortcut
  palette, and persisted operator preferences (filter defaults, reduced-motion).
