# AgentForge API Contract (v1)

Base URL: `/api`. All responses JSON unless noted. Auth via `Authorization: Bearer <JWT>`.
Errors: `{"detail": "message"}` with appropriate 4xx/5xx. Timestamps ISO-8601 UTC.

## Auth

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/auth/register` | `{email, password, full_name}` | `201 {access_token, token_type, user}` |
| POST | `/api/auth/login` | `{email, password}` | `200 {access_token, token_type, user}` |
| GET | `/api/auth/me` | — | `UserRead` |

`UserRead`: `{id, email, full_name, role, created_at}`. Demo credentials are seeded
(`demo@agentforge.io` / `demo1234` by default, see `.env`).

## Runs

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/runs` | `{objective, dataset?, auto_approve?}` | `202 RunRead` (workflow starts async) |
| GET | `/api/runs?limit=&offset=&status=` | — | `{items: RunRead[], total}` |
| GET | `/api/runs/{run_id}` | — | `RunRead` |
| GET | `/api/runs/{run_id}/tasks` | — | `TaskRead[]` |
| GET | `/api/runs/{run_id}/events?after_seq=0` | — | `RunEventRead[]` |
| GET | `/api/runs/{run_id}/events/stream?after_seq=0` | — | `text/event-stream` |
| GET | `/api/runs/{run_id}/report` | — | `{run_id, report, artifacts[]}` |
| GET | `/api/runs/{run_id}/artifacts` | — | `{items: ArtifactRead[]}` |
| GET | `/api/artifacts/{artifact_id}/download` | — | text/markdown file (Bearer auth) |
| POST | `/api/runs/{run_id}/cancel` | — | `RunRead` |
| POST | `/api/runs/{run_id}/approvals/{approval_id}` | `{decision: "approved"\|"rejected", note?}` | `RunRead` |

`RunRead`:
```json
{
  "id": "…", "objective": "…", "status": "queued|running|awaiting_approval|completed|failed|cancelled",
  "plan": {"objective": "...", "summary": "...", "tasks": [...], "success_criteria": [...]},
  "final_report": "markdown or null",
  "error": null, "created_at": "...", "started_at": "...", "completed_at": "...",
  "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
  "pending_approval": {"id": "...", "task_id": "...", "title": "...", "requested_action": "...", "reason": "..."} 
}
```

`TaskRead`: `{id, task_id, title, agent, status, attempts, result, evaluation}`.

`RunEventRead`: `{seq, run_id, type, ts, agent, task_id, message, data, duration_ms}`.

### SSE

`event: message` / `data: <RunEventRead JSON>` frames, heartbeat comment `: ping`
every 15s, closes when run reaches a terminal status. Reconnect with `after_seq`.
The browser must use `fetch` + `ReadableStream` (not `EventSource`) so it can send
the `Authorization: Bearer` header; missing/invalid auth returns 401.

## Agents / tools / knowledge

| Method | Path | Response |
|---|---|---|
| GET | `/api/agents` | `{agents: [{name, role, description, capabilities[], tools[]}]}` |
| GET | `/api/tools` | `{tools: [{name, description, category, sensitive, scopes[], input_schema}]}` |
| POST | `/api/knowledge/ingest` | multipart/form-data or JSON `{title, source, text, metadata?}` → `{chunks_added}` |
| GET | `/api/knowledge/search?q=&k=5` | `{results: [{title, source, text, score}]}` |
| GET | `/api/health` | `{status, version, llm_provider, llm_model, database, environment}` |
| GET | `/api/metrics/runs` | `{total_runs, completed, failed, success_rate, avg_duration_seconds, total_tokens, total_retries, avg_eval_score, by_status}` |

`ArtifactRead`: `{id, run_id, name, kind, path, description, created_at}`.
`GET /api/metrics/runs` aggregates across all runs visible to the authenticated user.

## Frontend integration rules

- Login form pre-fills demo credentials; store JWT in `localStorage`.
- Submit objective → `POST /api/runs` → navigate `/runs/{id}`.
- Live timeline: SSE first; if EventSource errors, fall back to polling
  `GET /events?after_seq`, every 1.5s.
- Approval banner: if `pending_approval`, show action + reason and approve/reject.
- Report panel renders Markdown. Show plan, tasks, agents, retries, evaluations, tokens.
- Scenario presets include the sales analysis request and a sensitive execution request.

## CORS

Allowed origins from `CORS_ORIGINS` (default includes `http://localhost:5173` and
`http://127.0.0.1:5173`, plus same-origin when served by FastAPI).
