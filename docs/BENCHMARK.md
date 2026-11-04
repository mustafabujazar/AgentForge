# AgentForge Benchmark

_Generated: 2026-11-04T20:43:59+00:00_

## What this measures

The benchmark drives the **real** workflow (LangGraph graph, planner, four role agents, tool registry, evaluator/retry loop, HITL approval gate, synthesizer and SQLite persistence) over fixed business objectives. It measures: runs completed / total, success rate, wall-clock latency (p50/p95 across scenarios), average evaluation score, retry count, total tokens, artifact/event production and an optional token-cost estimate.

> **OFFLINE SCRIPTED-LLM RUN — not a model-quality benchmark.** The LLM is replaced by a deterministic scripted provider (provider=`mock`, model=`benchmark-scripted`), so no network calls occur. These numbers measure orchestration/tool/persistence health and are reproducible on any machine; token counts are scripted word-count proxies and cost is $0.00. Real Groq numbers are never fabricated — run `python scripts/benchmark.py --provider groq` to produce them.

## Environment

- Python: `3.12.0`
- Provider / model: `mock` / `benchmark-scripted`
- Mode: `offline-scripted`
- Database: temporary sqlite (isolated)
- Artifacts: temporary directory
- Knowledge corpus chunks ingested: 92
- Scenarios run: 4 of 4

## Results by scenario

| Scenario | Status | Wall latency (s) | Tasks completed/total | Skipped | Max attempts | Retries | Avg eval | Tokens | LLM calls | Artifacts | Report chars |
|---|---|---|---|---|---|---|---|---|---|---|---|
| sales_performance_analysis | completed | 0.361 | 4/4 | 0 | 1 | 0 | 1.000 | 7480 | 10 | 3 | 1427 |
| revenue_root_cause_diagnosis | completed | 0.266 | 4/4 | 0 | 1 | 0 | 1.000 | 7480 | 10 | 3 | 1427 |
| remediation_plan_with_approval | completed | 0.315 | 4/4 | 0 | 1 | 0 | 1.000 | 7468 | 10 | 3 | 1427 |
| pricing_campaign_action | completed | 0.356 | 4/4 | 0 | 1 | 0 | 1.000 | 7486 | 10 | 3 | 1427 |

## Aggregate

| Metric | Value |
|---|---|
| Runs completed / total | 4 / 4 |
| Success rate | 1.00 |
| Wall latency p50 | 0.336 s |
| Wall latency p95 | 0.360 s |
| Average evaluation score | 1.000 |
| Retries (task attempts beyond the first) | 0 |
| Total tokens | 29914 |
| Cost estimate (USD) | 0.0000 |

- HITL scenarios: 2 approval-gated runs; 2 paused and resumed after a recorded approval decision.

## How to reproduce

```powershell
# offline deterministic (default; no network, no API key)
.\venv\Scripts\python.exe scripts/benchmark.py

# limit the scenario count
.\venv\Scripts\python.exe scripts/benchmark.py --limit 2

# real hosted-model run (requires GROQ_API_KEY in .env)
.\venv\Scripts\python.exe scripts/benchmark.py --provider groq --limit 4

# JSON summary only, no markdown rewrite
.\venv\Scripts\python.exe scripts/benchmark.py --no-doc
```

Each invocation uses a throwaway SQLite database and artifacts directory (the developer database is never touched) and ingests `data/knowledge` into an isolated RAG store.

## Limitations

- Offline mode replaces the LLM with a deterministic scripted provider: it verifies orchestration, tool execution, evaluation/retry plumbing and persistence, but says nothing about model quality or real-model latency.
- Latency p50/p95 are computed across the small scenario set (n=4), so they are indicative rather than statistical.
- The demo sales dataset and knowledge corpus are small; tool latency is not representative of production data volumes.
- Cost is only computed when `--cost-per-1m` is supplied; the default is 0.0 so no price is invented.

## Real provider results

_Not executed in this environment._ No real Groq numbers are recorded here because none were measured. Running `python scripts/benchmark.py --provider groq --limit 4` writes its actual results into this file (clearly marked as a real provider run).

