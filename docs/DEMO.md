# AgentForge Demo Guide

## The headline request

> Analyze our sales performance, identify the three worst-performing products,
> investigate why they're underperforming, and generate a report with recommendations.

## What the platform does with it

1. **Intake** records the objective against the authenticated user.
2. **Planner** turns it into an ordered plan — typically:
   1. Research internal context (quality, pricing, marketing memos) — *research*
   2. Quantify performance and isolate the bottom three products — *analyst*
   3. Explain root causes with evidence — *analyst*
   4. Draft an action plan — *execution*
   5. Write the executive report — *reporting*
3. **Router** checks each task against the agent capability registry; unknown agents
   are repaired to the closest supported role.
4. **Agents** run bounded tool loops: `sales_sql` for numbers, `knowledge_search` for
   internal evidence, `web_search` for simulated market context, `report_writer` for
   deliverables.
5. **Evaluator** scores each task with rules + an LLM judge; low scores retry with
   feedback (up to two times).
6. **Synthesizer** produces the final Markdown business report and persists it as an
   artifact.
7. The dashboard streams every step live over SSE: plan, tool calls, findings, scores,
   retries, token usage, final report.

## Expected ground truth (generated dataset)

The demo warehouse contains 18 months of sales for 8 products. The three
worst-performing products over the trailing 12 months and their distinct causes:

| Product | Trailing-12m revenue | Signature | Cause |
|---|---|---|---|
| Tide Portable Speaker | ~$139k | Units −12% YoY, marketing ~$1.1k/mo vs $4k floor, stable discount/returns | Under-invested marketing |
| Cirrus Air Purifier | ~$340k | Discount 5% → 35%, gross margin 62% → 44%, units up | Promo dependency eroding margin |
| Solstice Ergonomic Chair | ~$385k | Returns 2% → 18%, support tickets >10×, revenue −46% YoY | Quality defect / supplier issue |

The next product is ~$600k (clean gap), so "bottom three" is unambiguous. Internal
memos (`data/knowledge/`) contain the corresponding evidence: quality escalation
procedure, discount approval policy, marketing spend floor, supplier agreement and
support taxonomy.

## Run it

```powershell
# Backend + workflow (real Groq)
.\venv\Scripts\python.exe scripts\demo_run.py

# Or start the API and use the dashboard
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# frontend (dev): cd frontend; npm run dev  → http://localhost:5173
# frontend (built): cd frontend; npm run build → served at http://127.0.0.1:8000
```

Login with `demo@agentforge.io` / `demo1234`.

## Human-in-the-loop scenario

> Launch a 15% discount campaign for the worst-performing product and email the
> regional sales leads.

The planner marks the execution task sensitive; the run pauses at
`awaiting_approval`, the dashboard shows the exact requested action and reason, and
Approve/Reject resumes or skips the action before the final report. Analysis-only
requests do not require approval.

## What to look at in the UI

- **Run timeline** — every event with agent, task, tool call and duration.
- **Plan board** — tasks, dependencies, assigned agents, attempts, scores.
- **Approval banner** — action, reason, approve/reject.
- **Report panel** — final Markdown with evidence and recommendations.
- **Usage** — token totals and call counts per run.
- **Metrics** — success rate, latency, average evaluation score, retries.

## Demo limitations (say these out loud)

- The web-search tool is offline-safe and returns curated/simulated market notes,
  clearly marked `simulated: true`; it is not a live web crawler.
- HITL pause/resume uses an in-process LangGraph checkpointer; resuming after a
  process restart falls back to the persisted plan/state snapshot (documented).
- Local RAG uses deterministic lexical embeddings; production swaps in pgvector +
  real embeddings without changing agent code.
- Evaluation is LLM-as-judge plus rules, not human-labeled ground truth.
