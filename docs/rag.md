# RAG — Design & Implementation Notes

AgentForge grounds agent reasoning in the company's own documents. The retrieval
layer (`app/rag/`) chunks Markdown, embeds it with a **deterministic local model**,
persists vectors in the `knowledge_chunks` table, and answers hybrid similarity
queries. It runs offline with no new dependencies and works on both SQLite and
PostgreSQL.

## 1. Goals

* **Grounded answers** — the analyst/research agents retrieve pricing, quality,
  marketing and QBR evidence instead of inventing company facts.
* **Offline & deterministic** — identical corpus + query produce identical chunks,
  vectors and rankings across machines and restarts. No embedding API, no network.
* **No new packages** — standard library + SQLAlchemy 2.x only (numpy is *not*
  installed in the project venv; nothing here needs it).
* **Portable persistence** — vectors live in a JSON column, so production Postgres
  works without the pgvector extension. pgvector remains the documented scale-up path.
* **Restart-safe** — vectors are persisted; process restart does not require
  re-embedding, only an IDF refit from persisted text for query encoding.

## 2. Module layout

| Module | Responsibility |
|---|---|
| `app/rag/types.py` | `Chunk` (chunker output) and `RetrievedChunk` (`chunk_id, doc_id, title, source, text, score, metadata`). |
| `app/rag/chunking.py` | Markdown-aware, heading-aware chunker (~400–800 chars). |
| `app/rag/embeddings.py` | `LocalHashEmbedder`: hashed n-gram TF-IDF, 384-dim, L2-normalized; cosine + lexical similarity. |
| `app/rag/store.py` | `KnowledgeStore` + `get_knowledge_store()` singleton, ingestion, persistence, search. |
| `app/rag/cli.py` | `python -m app.rag.cli ingest|search|stats|clear`. |
| `app/rag/__init__.py` | Public exports (`get_knowledge_store`, `ingest_text`, `ingest_directory`, `search`, types). |

## 3. Public API

```python
from app.rag import get_knowledge_store, ingest_directory, ingest_text, search

store = get_knowledge_store()                 # process-wide singleton
store.ingest_text(title, source, text, metadata=None) -> int   # chunks added (0 if unchanged)
store.ingest_directory("data/knowledge")      # recursive **/*.md, sorted, -> int
store.search("why are chair returns rising?", k=5, min_score=0.08)
# -> list[RetrievedChunk(chunk_id, doc_id, title, source, text, score, metadata)]
```

Module-level `ingest_text(...)`, `ingest_directory(...)` and `search(...)` delegate
to the singleton. `get_knowledge_store(session_factory=..., force_reload=True)` lets
tests/scripts swap the DB session factory. `reset_knowledge_store()` clears the cache.

The store resolves the default session factory lazily from
`app.db.base.get_session_factory()`, so importing `app.rag` never opens a database
connection. The schema is created on first use via `init_db()` when using the app
database; tests pass their own session factory and create tables themselves.

## 4. Markdown-aware chunking (`chunking.py`)

1. **Section split** on ATX headings (`#`..`######`) while tracking the heading
   hierarchy; fenced code blocks are respected (a `#` line inside ``` is code, not a
   heading).
2. **Atomic blocks** — paragraphs, bullet/numbered lists, Markdown tables and fenced
   code blocks are never split mid-block.
3. **Packing** — blocks are greedily packed to a ~700-char target
   (`Settings.rag_chunk_size`), with a 55% fill threshold to avoid tiny chunks and a
   ~100-char sentence overlap (`Settings.rag_chunk_overlap`) when an over-long block
   must be split. A final pass merges tiny trailing chunks.
4. **Heading context** — every chunk text is prefixed with its breadcrumb:
   `Pricing & Discount Policy (FY26) > 4. Gross margin floor`. Metadata also stores
   `headings`, `section`, `chunk_index`, `char_length` and the document `file`.

This means a retrieved snippet is self-describing and can be quoted without the
surrounding document — important because agents see only the passages.

## 5. Deterministic local embeddings (`embeddings.py`)

`LocalHashEmbedder(dim=384)` is a hashed n-gram TF-IDF vectorizer:

1. **Tokenization** — lower-case word unigrams, word bigrams (context), light suffix
   stemming (`returns`/`returned` → `return`, `ticketing` → `ticket`), and character
   4/5-grams for words ≥ 6 chars (typo/morphology robustness). A small stopword list
   is removed.
2. **Feature hashing** — each feature is hashed with `hashlib.blake2b` (8-byte
   digest, *stable across processes*, unlike Python's salted `hash()`) into one of
   384 buckets with a sign bit. This is the hashing trick: collisions are bounded and
   deterministic.
3. **TF-IDF** — sub-linear TF (`1 + ln(count)`), multiplied by IDF fitted on the
   ingested corpus (`ln((1+N)/(1+df)) + 1`), summed per bucket with the hash sign.
4. **L2 normalization** — every vector is unit length, so cosine similarity is a dot
   product; values are rounded to 6 decimals for compact, byte-stable JSON storage.

There is **no numpy and no network**. A 384-float vector is ~3 KB of JSON; 92 chunks
are ~250 KB of embedding JSON in SQLite. `fit()` is deterministic; `embed()` is
deterministic (verified by tests: two instances produce identical vectors).

Why hashed TF-IDF instead of a neural embedding? It is zero-dependency, offline,
instantly reproducible and adequate for a small, jargon-rich internal corpus where
exact terms (`Solstice`, `NS-2602`, `gross margin floor`) matter more than semantic
paraphrase. The `Embedder` protocol (`dim`, `fit`, `embed`, `embed_many`) makes a
neural/API embedder a drop-in upgrade later.

## 6. Hybrid retrieval

`store.search()` scores every persisted chunk with:

```
score = 0.78 * cosine(query_vec, chunk_vec) + 0.22 * lexical(query, title + chunk_text)
```

* **cosine** — hashed TF-IDF cosine (the primary, contract-level signal).
* **lexical** — IDF-weighted cosine over exact word/stem overlap. It rewards rare
  exact matches (product names, SKU ids) that hashed collisions can blur. Component
  scores are exposed as `metadata.score_components`.

Results are filtered by `min_score` (default `Settings.rag_min_score` = 0.08 in the
tool), sorted descending, and truncated to `k`. Empty queries return `[]`; an empty
store returns `[]` rather than raising.

## 7. Persistence and idempotency

Table (defined in `app/db/models.py`, Lead):

```
knowledge_chunks(
  id, doc_id, chunk_index, title, source, text,
  embedding_json JSON, metadata_json JSON, created_at
  UNIQUE(doc_id, chunk_index)
)
```

* `doc_id = sha1(f"{source}|{title}")[:16]` — stable document identity.
* Ingesting changed text **replaces** that document's chunks (delete + insert in one
  transaction); ingesting unchanged text is a **no-op returning 0**. Startup seeding
  is therefore safe to run on every boot.
* Because IDF is corpus-wide, ingesting a new document refits IDF and re-embeds the
  remaining chunks so all persisted vectors stay on the same scale. The corpus is
  small (tens to low thousands of chunks); this is a few milliseconds.
* Re-opening the store refits IDF from persisted text (no re-embedding) so query
  vectors are consistent with stored ones.
* The store uses portable SQLAlchemy constructs only (`select`, `delete`, `func.count`,
  `JSON` columns) — no SQLite-specific SQL, no pgvector dependency.

## 8. Ingestion paths

| Path | Used by |
|---|---|
| `store.ingest_directory(settings.knowledge_path)` | `app/db/seed.py` startup seed (only when the table is empty) |
| `POST /api/knowledge/ingest` → `store.ingest_text(...)` | api-db knowledge router |
| `python -m app.rag.cli ingest data/knowledge` | local setup / reindex |
| `python -m app.rag.cli search "..."` | debugging retrieval |
| `scripts/generate_demo_data.py` | writes the corpus (`scripts/demo_knowledge.py`) |

The demo corpus has **9 internal memos + 4 market notes = 13 documents → 92 chunks**.
Every causal story has evidence in at least three documents (e.g. Solstice: quality
escalation memo §4, supplier agreement §2, product notes, returns policy, QBR §3).
The `market/` notes are a separate external corpus surfaced by `web_search` (always
`simulated: true`); they cover **2025–2026 only** to stay date-consistent with the
sales window and are deliberately product-agnostic so external benchmarks cannot be
mapped onto specific internal SKUs at retrieval time.

## 9. Retrieval examples (real output)

```
query: "gross margin floor and discount authority policy"
  top-3: pricing_policy.md (0.417) - "4. Gross margin floor", "3. Discount authority matrix", "1. Scope"

query: "Solstice chair supplier defect returns support tickets"
  top-3 sources: returns_and_warranty_policy.md (0.368), quality_escalation.md (0.291),
                 quarterly_business_review.md (0.280)

query: "Tide portable speaker marketing budget conversion"
  top-3 sources: market/portable_audio.md (0.298), marketing_playbook.md (0.293),
                 quarterly_business_review.md (0.292)

query: "bottom three SKUs by trailing twelve month revenue"
  top-2: quarterly_business_review.md §2 "Bottom three SKUs (trailing 12 months)" (0.240),
         metrics_definitions.md §3 "Canonical SQL - bottom SKUs by trailing-12-month revenue" (0.238)
```

Scores (0–1) are relative relevance indicators, not calibrated probabilities; the
tool applies `min_score = 0.08` by default to drop weak, unrelated passages.

## 10. Measured performance (demo corpus, Windows, SQLite)

| Operation | Result |
|---|---|
| Ingest 13 docs → 92 chunks (fresh, cold) | **~0.70 s** (includes IDF fit + embedding) |
| Warm search (k=5, 92 chunks) | **~66 ms** (DB load + hybrid scoring) |
| First search after restart (incl. IDF fit) | **~110 ms** |
| Storage | ~1.8 MB demo sales DB; knowledge chunks are a few hundred KB |

66 ms/query is comfortably inside the orchestrator's step budget. At much larger
corpora, loading all vectors per query stops being appropriate — that is what the
pgvector path below is for. (A simple in-process vector cache is an easy next step.)

## 11. Production path: PostgreSQL + pgvector

The deployed stack runs PostgreSQL 16 + pgvector (Docker). The current store is
production-compatible via JSON vectors; to scale retrieval:

1. **Schema upgrade** — add `embedding vector(384)` to `knowledge_chunks` (or a
   separate table), keep `embedding_json` for backfill/fallback. Add
   `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`.
2. **Ingest** — write the same vector into the `vector` column
   (`pgvector` SQLAlchemy type or raw SQL `CAST(:vec AS vector)`).
3. **Search** — replace the in-Python scoring loop with a SQL query:
   `SELECT ..., 1 - (embedding <=> :qvec) AS score FROM knowledge_chunks ORDER BY embedding <=> :qvec LIMIT :k`
   (cosine distance `<=>`; hybrid lexical can be fused with `ts_rank` over a
   `to_tsvector('english', title || ' ' || text)` column).
4. **Embeddings** — swap `LocalHashEmbedder` for an API/neural embedder behind the
   `Embedder` protocol, keeping `dim` configurable; reindex once with
   `python -m app.rag.cli clear && ... ingest`.
5. **Fallback** — SQLite/JSON path stays for local dev and CI (no extension needed);
   the `KnowledgeStore` interface and `RetrievedChunk` shape are unchanged.

No application code outside `app/rag/store.py` needs to change: the tool
(`knowledge_search`), API router and agents depend only on `store.search()` and
`RetrievedChunk`.

## 12. Determinism, safety, tests

* **No network** — no embedding provider, no live web. `web_search` is a separate,
  clearly-simulated market corpus.
* **Deterministic** — `blake2b` hashing, sorted file traversal, no randomness; the
  chunker is pure. Same corpus → same chunk ids' content and same ranking.
* **Untrusted content** — retrieved text is data, never instructions; agents are
  prompted to cite, not obey, documents. Vectors are stored as JSON, so a malformed
  document cannot corrupt the index.
* **Tests** — `tests/test_tools_rag.py` covers chunking invariants (heading context,
  intact tables/code), embedding determinism/normalization, ingest idempotency,
  persistence across store instances, relevance for all three story queries,
  `min_score` filtering, empty-store behavior, and the `knowledge_search` tool
  against a temporary database.

## 13. Limitations & next steps

* Hashed TF-IDF is lexical, not semantic: paraphrases with no shared terms can be
  missed. Neural embeddings (pgvector path) fix this.
* IDF refit requires re-embedding the corpus on ingest; fine for a document set of
  this size, not for continuous high-volume ingestion. A background reindex queue is
  the production fix.
* No cross-encoder reranker; the lexical component is a cheap proxy.
* No multi-tenant isolation; production should partition `knowledge_chunks` by
  workspace/tenant and filter at query time.
* Chunking targets ~700 chars; very long tables are split on block boundaries.
