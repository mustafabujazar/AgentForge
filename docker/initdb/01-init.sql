-- ---------------------------------------------------------------------------
-- AgentForge — PostgreSQL initialisation (runs once on a fresh pgdata volume)
--
-- Executed automatically by the `pgvector/pgvector:pg16` image via
--   /docker-entrypoint-initdb.d/01-init.sql
-- Only extensions live here: application tables are created by SQLAlchemy
-- (`Base.metadata.create_all`) on first startup — see docs/deployment.md.
-- ---------------------------------------------------------------------------

-- Vector similarity for the production RAG path (ARCHITECTURE.md §4.3).
-- The pgvector image already ships the extension binary; this makes it
-- available in the `agentforge` database.
CREATE EXTENSION IF NOT EXISTS vector;

-- Useful for case-insensitive lookups in future knowledge/auth queries.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Schema notes (no application DDL — owned by SQLAlchemy models)
--
-- The ORM models (app/db/models.py) create:
--   users, runs, task_records, run_events, approvals, artifacts,
--   knowledge_chunks, evaluation_metrics
--
-- `runs.plan_json` / `runs.state_json` / `run_events.data_json` hold JSON
-- snapshots; SQLite stores the same JSON as TEXT. Keep both JSON encodings
-- portable (stdlib json). Embeddings currently live in
-- `knowledge_chunks.embedding_json` (JSON list of floats).
--
-- RAG: ARCHITECTURE.md §4.3 declares pgvector as the production path, but the
-- current code (and requirements.txt) has no `pgvector` Python package, so
-- embeddings are stored as JSON/TEXT arrays and ranked with local cosine
-- similarity in the vector store. This extension is provisioned so the
-- migration to native `vector(N)` columns is a model change only — no
-- infrastructure work:
--
--   ALTER TABLE knowledge_chunks
--       ADD COLUMN embedding_vector vector(384);
--   CREATE INDEX ON knowledge_chunks
--       USING hnsw (embedding_vector vector_cosine_ops);
--
-- 384 is the current local (pure-stdlib) embedding dimension (EMBEDDING_DIM);
-- keep the column dimension in lockstep with the embedding implementation.
--
-- Migrations: there is intentionally no Alembic setup. `create_all` only
-- creates missing tables and does not alter existing ones. For destructive
-- schema changes, back up the volume, then recreate (see deployment.md
-- "Rollback & upgrades").
-- ---------------------------------------------------------------------------
