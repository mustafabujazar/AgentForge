"""Tests for the AgentForge tools + RAG layer (owner: tools-rag).

Self-contained: every test uses temporary paths / a temporary SQLite database, so
the suite never mutates the committed demo data or the app database. The only
shared module-scoped artifacts are the generated demo dataset and the ingested
knowledge index (both in pytest temp directories).

Run: ``.\\venv\\Scripts\\python.exe -m pytest tests/test_tools_rag.py -q``
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # make ``app`` and ``scripts`` importable
    sys.path.insert(0, str(ROOT))

EXPECTED_TOOLS = {
    "sales_sql",
    "csv_query",
    "knowledge_search",
    "web_search",
    "report_writer",
}

BOTTOM3_NAMES = ["Tide Portable Speaker", "Cirrus Air Purifier", "Solstice Ergonomic Chair"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> dict:
    """Generate the demo dataset + corpus once into a temp root."""
    from scripts.generate_demo_data import generate

    root = tmp_path_factory.mktemp("agentforge-demo")
    return generate(root, quiet=True)


@pytest.fixture(scope="module")
def demo_db(generated) -> Path:
    return Path(generated["db_path"])


@pytest.fixture(scope="module")
def knowledge_dir(generated) -> Path:
    return Path(generated["knowledge_dir"])


@pytest.fixture()
def tool_ctx(tmp_path):
    from app.core.config import get_settings
    from app.tools.base import ToolContext

    return ToolContext(
        run_id="pytest-run",
        agent="analyst",
        settings=get_settings(),
        artifacts_dir=tmp_path / "artifacts",
        extras={},
    )


def _execute(name: str, args: dict, ctx, scopes=None):
    from app.tools.registry import get_registry

    return get_registry().execute(name, args, ctx, allowed_scopes=scopes)


def _db_fingerprint(db_path: Path) -> dict[str, list[tuple]]:
    connection = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in tables
            if not table.startswith("sqlite_")
        }
    finally:
        connection.close()


def _file_hashes(directory: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            hashes[path.relative_to(directory).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


# ---------------------------------------------------------------------------
# A. Generator: schema, determinism, story evidence
# ---------------------------------------------------------------------------

def test_generator_schema_and_counts(generated, demo_db):
    assert demo_db.exists() and demo_db.stat().st_size > 100_000
    connection = sqlite3.connect(str(demo_db))
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"products", "sales", "marketing_spend", "support_tickets"} <= tables

        products = [dict(row) for row in connection.execute("SELECT * FROM products")]
        assert len(products) == 8
        assert {row["product_id"] for row in products} == set(range(1, 9))
        assert all(row["unit_price"] > 0 and row["unit_cost"] > 0 for row in products)

        sales_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(sales)")
        }
        assert {
            "sale_id",
            "product_id",
            "region",
            "channel",
            "quantity",
            "revenue",
            "cost",
            "discount",
            "returned",
            "sale_date",
        } <= sales_columns
        assert {
            "campaign_id",
            "product_id",
            "month",
            "spend",
        } <= {row["name"] for row in connection.execute("PRAGMA table_info(marketing_spend)")}
        assert {
            "ticket_id",
            "product_id",
            "issue_type",
            "severity",
            "opened_at",
        } <= {row["name"] for row in connection.execute("PRAGMA table_info(support_tickets)")}

        sales_count = connection.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        assert sales_count > 5_000
        assert connection.execute("SELECT COUNT(*) FROM marketing_spend").fetchone()[0] == 8 * 18
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] > 200

        start, end = connection.execute(
            "SELECT MIN(sale_date), MAX(sale_date) FROM sales"
        ).fetchone()
        assert start == "2025-05-01" and end == "2026-10-31"
        regions = {row[0] for row in connection.execute("SELECT DISTINCT region FROM sales")}
        assert regions == {"North", "South", "East", "West"}
        assert connection.execute(
            "SELECT COUNT(*) FROM sales WHERE quantity <= 0 OR revenue <= 0 OR discount < 0 OR discount >= 0.5"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sales WHERE returned NOT IN (0, 1)"
        ).fetchone()[0] == 0
        # Every marketing month is a real YYYY-MM string and every product has 18.
        assert connection.execute(
            "SELECT COUNT(*) FROM marketing_spend WHERE month NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'"
        ).fetchone()[0] == 0
    finally:
        connection.close()

    assert generated["row_counts"]["products"] == 8
    assert generated["row_counts"]["sales"] == sales_count  # summary matches the DB
    assert generated["row_counts"]["sales"] > 5_000
    assert len(generated["knowledge_files"]) == 13


def test_generator_is_deterministic_and_idempotent(tmp_path):
    from scripts.generate_demo_data import generate

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = generate(first_root, quiet=True)
    second = generate(second_root, quiet=True)

    assert first["row_counts"] == second["row_counts"]
    assert _db_fingerprint(Path(first["db_path"])) == _db_fingerprint(Path(second["db_path"]))
    assert _file_hashes(first_root / "data" / "knowledge") == _file_hashes(second_root / "data" / "knowledge")
    for name in ("products.csv", "monthly_product_metrics.csv", "region_month_metrics.csv"):
        assert (first_root / "data" / "demo" / name).read_bytes() == (
            second_root / "data" / "demo" / name
        ).read_bytes()

    # Re-running the same root must not accumulate rows or duplicate knowledge files.
    before = _db_fingerprint(Path(first["db_path"]))
    knowledge_before = _file_hashes(first_root / "data" / "knowledge")
    generate(first_root, quiet=True)
    assert _db_fingerprint(Path(first["db_path"])) == before
    assert _file_hashes(first_root / "data" / "knowledge") == knowledge_before


def test_generator_bottom_three_products(generated):
    bottom3 = generated["bottom3_ttm"]
    assert [item["name"] for item in bottom3] == BOTTOM3_NAMES
    revenues = [item["ttm_revenue"] for item in bottom3]
    assert revenues == sorted(revenues)
    # A clean gap to the next-best SKU (Lumen ~$0.6M) makes the objective unambiguous.
    all_products = {item["name"]: item["ttm_revenue"] for item in generated["products"]}
    assert all_products["Lumen Smart Bulb 4-Pack"] > revenues[-1] * 1.4
    assert all_products["Nimbus Wireless Headphones"] > 1_000_000


def test_generator_three_distinct_causal_stories(generated):
    evidence = generated["story_evidence"]

    quality = evidence["quality_defect"]
    assert quality["product"] == "Solstice Ergonomic Chair"
    assert quality["return_rate_baseline"] < 0.05
    assert quality["return_rate_latest"] > 0.15
    assert quality["tickets_per_month_latest"] > 5 * quality["tickets_per_month_baseline"]
    assert quality["revenue_change_pct"] < -0.35
    assert quality["marketing_avg_ttm"] > 3_000  # marketing was not cut
    assert quality["discount_rate_latest"] < 0.15  # and price was not cut

    pricing = evidence["discount_dependency"]
    assert pricing["product"] == "Cirrus Air Purifier"
    assert pricing["discount_rate_late"] > pricing["discount_rate_early"] + 0.2
    assert pricing["gross_margin_early"] > 0.58
    assert pricing["gross_margin_late"] < 0.50
    assert pricing["return_rate_latest"] < 0.10  # quality is not the issue

    marketing = evidence["weak_marketing"]
    assert marketing["product"] == "Tide Portable Speaker"
    assert marketing["marketing_avg_ttm"] < 2_000
    assert abs(marketing["discount_rate_latest"] - marketing["discount_rate_baseline"]) < 0.03
    assert marketing["units_change_pct"] < -0.05
    assert marketing["return_rate_latest"] < 0.06


def test_knowledge_corpus_contains_evidence(generated, knowledge_dir):
    files = sorted(knowledge_dir.rglob("*.md"))
    assert len(files) == 13
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in files)
    for keyword in (
        "Solstice",
        "Cirrus",
        "Tide",
        "Nordic Spring",
        "gross margin floor",
        "viability floor",
        "discount dependency",
        "returns",
        "support tickets",
        "trailing-12-month",
    ):
        assert keyword.lower() in corpus.lower(), f"missing evidence keyword: {keyword}"
    titles = {
        path.relative_to(knowledge_dir).as_posix(): path.read_text(encoding="utf-8").strip().splitlines()[0]
        for path in files
    }
    assert all(line.startswith("# ") for line in titles.values())
    market_dir = knowledge_dir / "market"
    assert market_dir.is_dir()
    market_files = sorted(market_dir.glob("*.md"))
    assert len(market_files) == 4
    # The external corpus must stay date-consistent with the demo window and must not
    # name internal SKUs (so agents cannot map external benchmarks onto demo products
    # or invent 2024/blender/kettle-style sources).
    market_text = "\n".join(path.read_text(encoding="utf-8") for path in market_files).lower()
    assert "2025" in market_text and "2026" in market_text
    assert "2024" not in market_text
    for internal in ("solstice", "cirrus", "tide", "ns-2602", "blender", "kettle", "home appliance"):
        assert internal not in market_text, f"market notes must be product-agnostic; found {internal!r}"
    for title in (
        path.read_text(encoding="utf-8").strip().splitlines()[0] for path in market_files
    ):
        assert "2025" in title and "2026" in title


# ---------------------------------------------------------------------------
# B. Registry + sales_sql
# ---------------------------------------------------------------------------

def test_registry_contract_and_specs():
    from app.tools.registry import get_registry

    registry = get_registry()
    assert set(registry.tool_names()) == EXPECTED_TOOLS
    for spec in registry.list_specs():
        payload = spec.to_dict()
        json.dumps(payload)  # must be JSON-serializable for agents/API
        assert payload["description"]
        assert isinstance(payload["input_schema"].get("properties"), dict)
        assert isinstance(payload["scopes"], list)
        assert spec.sensitive is False
    assert registry.get("sales_sql").spec.input_schema["required"] == ["query"]
    assert registry.get("report_writer").spec.input_schema["required"] == ["title", "content"]


def _ctx_for_sql(tool_ctx, demo_db):
    tool_ctx.extras["sales_db_path"] = str(demo_db)
    return tool_ctx


def test_sales_sql_answers_bottom_three(generated, demo_db, tool_ctx):
    ctx = _ctx_for_sql(tool_ctx, demo_db)
    result = _execute(
        "sales_sql",
        {
            "query": """
                SELECT p.name, ROUND(SUM(s.revenue), 2) AS ttm_revenue
                FROM sales s
                JOIN products p ON p.product_id = s.product_id
                WHERE s.sale_date BETWEEN '2025-11-01' AND '2026-10-31'
                GROUP BY p.product_id
                ORDER BY ttm_revenue ASC
                LIMIT 3
            """
        },
        ctx,
    )
    assert result.ok, result.error
    assert result.output["columns"] == ["name", "ttm_revenue"]
    assert result.output["row_count"] == 3
    assert [row["name"] for row in result.output["rows"]] == BOTTOM3_NAMES
    assert result.output["truncated"] is False


def test_sales_sql_rejects_unsafe_statements(demo_db, tool_ctx):
    ctx = _ctx_for_sql(tool_ctx, demo_db)
    unsafe = [
        "INSERT INTO sales (sale_id) VALUES (999999)",
        "UPDATE sales SET revenue = 0",
        "DELETE FROM sales",
        "DROP TABLE sales",
        "CREATE TABLE hacked (x INTEGER)",
        "ALTER TABLE sales ADD COLUMN hacked TEXT",
        "PRAGMA table_info(sales)",
        "SELECT 1; DELETE FROM sales",
        "ATTACH DATABASE 'evil.db' AS evil",
        "/* comment */ DELETE FROM products",
        "",
    ]
    for statement in unsafe:
        result = _execute("sales_sql", {"query": statement}, ctx)
        assert result.ok is False, f"unsafe statement accepted: {statement!r}"
        assert result.error

    # A write cannot sneak through via SQLite returning a result set.
    assert _execute("sales_sql", {"query": "SELECT 1; SELECT 2"}, ctx).ok is False
    # A `;` inside a string literal or a comment is not a second statement.
    safe = _execute(
        "sales_sql",
        {"query": "SELECT COUNT(*) AS n FROM products WHERE name = 'Aurora; Arc Lamp' -- ok"},
        ctx,
    )
    assert safe.ok, safe.error
    assert safe.output["rows"][0]["n"] == 0


def test_sales_sql_row_limit_and_scope_enforcement(demo_db, tool_ctx):
    ctx = _ctx_for_sql(tool_ctx, demo_db)
    limited = _execute("sales_sql", {"query": "SELECT * FROM sales", "limit": 10}, ctx)
    assert limited.ok
    assert limited.output["row_count"] == 10
    assert limited.output["truncated"] is True

    oversized = _execute("sales_sql", {"query": "SELECT * FROM sales", "limit": 5000}, ctx)
    assert oversized.ok
    assert oversized.output["limit"] == 200
    assert oversized.output["row_count"] == 200

    denied = _execute(
        "sales_sql",
        {"query": "SELECT 1"},
        ctx,
        scopes={"read:knowledge"},
    )
    assert denied.ok is False
    assert "scope" in (denied.error or "").lower()

    missing_args = _execute("sales_sql", {}, ctx)
    assert missing_args.ok is False


def test_sales_sql_missing_database_fails_cleanly(tool_ctx, tmp_path):
    tool_ctx.extras["sales_db_path"] = str(tmp_path / "does-not-exist.db")
    result = _execute("sales_sql", {"query": "SELECT 1"}, tool_ctx)
    assert result.ok is False
    assert "not found" in (result.error or "")


# ---------------------------------------------------------------------------
# C. csv_query
# ---------------------------------------------------------------------------

def _ctx_for_csv(tool_ctx, demo_db):
    tool_ctx.extras["csv_dir"] = str(demo_db.parent)
    return tool_ctx


def test_csv_query_describe_and_aggregate(demo_db, tool_ctx):
    ctx = _ctx_for_csv(tool_ctx, demo_db)
    described = _execute(
        "csv_query", {"file": "monthly_product_metrics.csv", "operation": "describe"}, ctx
    )
    assert described.ok, described.error
    assert described.output["total_rows"] == 144
    assert "revenue" in described.output["numeric_columns"]
    assert any(row["column"] == "gross_margin" for row in described.output["rows"])

    aggregated = _execute(
        "csv_query",
        {
            "file": "monthly_product_metrics",
            "operation": "aggregate",
            "group_by": "product_name",
            "metric": "sum",
            "column": "revenue",
            "limit": 3,
        },
        ctx,
    )
    assert aggregated.ok, aggregated.error
    rows = aggregated.output["rows"]
    assert len(rows) == 3
    assert rows[0]["group"] == "Nimbus Wireless Headphones"
    assert "sum(revenue)" in rows[0]
    assert rows[0]["sum(revenue)"] >= rows[1]["sum(revenue)"] >= rows[2]["sum(revenue)"]

    filtered = _execute(
        "csv_query",
        {
            "file": "monthly_product_metrics",
            "operation": "aggregate",
            "group_by": "month",
            "metric": "count",
            "limit": 5,
        },
        ctx,
    )
    assert filtered.ok, filtered.error
    assert filtered.output["row_count"] == 5

    head = _execute("csv_query", {"file": "products", "operation": "head", "limit": 2}, ctx)
    assert head.ok
    assert head.output["row_count"] == 2


def test_csv_query_rejects_paths_and_bad_columns(demo_db, tool_ctx):
    ctx = _ctx_for_csv(tool_ctx, demo_db)
    for bad_file in ("../sales.db", "..\\sales.db", "C:/Windows/system.ini", "unknown"):
        result = _execute("csv_query", {"file": bad_file}, ctx)
        assert result.ok is False, f"accepted unsafe csv file: {bad_file!r}"

    unknown_column = _execute(
        "csv_query",
        {
            "file": "monthly_product_metrics",
            "operation": "aggregate",
            "group_by": "does_not_exist",
            "metric": "sum",
            "column": "revenue",
        },
        ctx,
    )
    assert unknown_column.ok is False
    assert "group_by" in (unknown_column.error or "")


# ---------------------------------------------------------------------------
# D. RAG: chunking, embeddings, store, relevance
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rag_session_factory(tmp_path_factory):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base

    db_path = tmp_path_factory.mktemp("rag-db") / "rag.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture(scope="module")
def knowledge_store(rag_session_factory, knowledge_dir):
    from app.rag.store import KnowledgeStore, reset_knowledge_store

    reset_knowledge_store()
    store = KnowledgeStore(rag_session_factory)
    added = store.ingest_directory(knowledge_dir)
    assert added > 40, "knowledge corpus produced too few chunks"
    yield store
    reset_knowledge_store()


def test_markdown_chunking_keeps_heading_context_and_blocks():
    from app.rag.chunking import chunk_markdown

    markdown = """# Pricing Policy

## 1. Scope

Short paragraph about scope and discounts.

## 2. Discount matrix

| Discount | Approver |
|---|---|
| 0-5% | AE |
| 25%+ | CFO |

## 3. Example query

```sql
SELECT name FROM products;
```

Final paragraph with a long explanation that should remain readable.
"""
    chunks = chunk_markdown(markdown, title="Pricing Policy", chunk_size=400, overlap=50)
    assert chunks
    for chunk in chunks:
        assert chunk.text.startswith("Pricing Policy"), chunk.text[:60]
        assert chunk.metadata["headings"]
        assert chunk.title == "Pricing Policy"
        assert 40 <= len(chunk.text) <= 1_200
    table_chunk = next(chunk for chunk in chunks if "| Discount |" in chunk.text)
    assert "| 25%+ | CFO |" in table_chunk.text  # table kept intact
    sql_chunk = next(chunk for chunk in chunks if "SELECT name" in chunk.text)
    assert sql_chunk.text.count("```") in (0, 2)  # fence not split mid-block


def test_embeddings_are_deterministic_and_normalized(knowledge_dir):
    from app.rag.embeddings import LocalHashEmbedder, cosine_similarity

    corpus = [
        "Solstice chair returns and supplier defect",
        "Cirrus air purifier discount dependency margin collapse",
        "Tide portable speaker marketing budget conversion",
    ]
    first = LocalHashEmbedder(dim=384).fit(corpus)
    second = LocalHashEmbedder(dim=384).fit(corpus)
    vector_a = first.embed("Solstice chair defect returns")
    vector_b = second.embed("Solstice chair defect returns")
    assert len(vector_a) == 384
    assert vector_a == vector_b  # deterministic across embedder instances
    norm = sum(value * value for value in vector_a) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)
    assert cosine_similarity(vector_a, vector_b) == pytest.approx(1.0, abs=1e-6)

    same_topic = first.embed("chair defect returns supplier")
    other_topic = first.embed("air purifier discount promotion margin")
    assert cosine_similarity(vector_a, same_topic) > cosine_similarity(vector_a, other_topic)
    # Empty text yields a zero vector rather than raising.
    assert first.embed("") == [0.0] * 384


def test_store_ingest_is_idempotent(knowledge_store, knowledge_dir):
    chunk_count = knowledge_store.count_chunks()
    stats = knowledge_store.stats()
    assert stats["documents"] == 13
    assert chunk_count >= 80
    assert knowledge_store.ingest_directory(knowledge_dir) == 0
    assert knowledge_store.count_chunks() == chunk_count


def test_store_search_relevance(knowledge_store):
    pricing_hits = knowledge_store.search("gross margin floor and discount authority policy", k=5)
    assert pricing_hits, "no hits for pricing query"
    assert pricing_hits[0].source == "pricing_policy.md"
    assert any(hit.source == "pricing_policy.md" for hit in pricing_hits[:3])

    chair_hits = knowledge_store.search("Solstice chair supplier defect returns support tickets", k=5)
    chair_sources = {hit.source for hit in chair_hits[:3]}
    assert chair_sources & {
        "quality_escalation.md",
        "product_notes.md",
        "quarterly_business_review.md",
        "returns_and_warranty_policy.md",
    }, chair_sources
    assert any("solstice" in hit.text.lower() for hit in chair_hits[:5])

    marketing_hits = knowledge_store.search("Tide portable speaker marketing budget conversion", k=5)
    marketing_sources = {hit.source for hit in marketing_hits[:3]}
    assert marketing_sources & {
        "marketing_playbook.md",
        "product_notes.md",
        "quarterly_business_review.md",
    }, marketing_sources

    for hit in chair_hits:
        assert hit.title and hit.source and hit.text and hit.doc_id and hit.chunk_id
        assert -1.0 <= hit.score <= 1.0
        assert isinstance(hit.metadata, dict)


def test_store_search_min_score_and_empty_store(rag_session_factory, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.rag.store import KnowledgeStore

    filtered = KnowledgeStore(rag_session_factory)
    assert filtered.count_chunks() > 0
    high_threshold = filtered.search("gross margin floor", k=10, min_score=0.95)
    assert high_threshold == []

    engine = create_engine(f"sqlite:///{(tmp_path / 'empty.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    try:
        empty_store = KnowledgeStore(sessionmaker(bind=engine, expire_on_commit=False))
        assert empty_store.search("anything", k=3) == []
        assert empty_store.count_chunks() == 0
    finally:
        engine.dispose()


def test_store_persists_across_instances(rag_session_factory):
    from app.rag.store import KnowledgeStore

    reopened = KnowledgeStore(rag_session_factory)
    hits = reopened.search("quality escalation defect Nordic Spring", k=3)
    assert hits
    assert hits[0].source in {"quality_escalation.md", "product_notes.md", "quarterly_business_review.md"}


def test_ingest_text_round_trip(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.rag.store import KnowledgeStore

    engine = create_engine(f"sqlite:///{(tmp_path / 'roundtrip.db').as_posix()}")
    Base.metadata.create_all(engine)
    try:
        store = KnowledgeStore(sessionmaker(bind=engine, expire_on_commit=False))
        text = (
            "# Holiday Fulfillment Memo\n\n## 1. Cutoff dates\n\n"
            "Orders placed after December 15 ship in January.\n\n"
            "## 2. Carrier capacity\n\nAir freight capacity is reserved for the Nimbus phone launch.\n"
        )
        added = store.ingest_text("Holiday Fulfillment Memo", "holiday_memo.md", text, metadata={"team": "ops"})
        assert added >= 1
        assert store.count_chunks() == added
        hits = store.search("December 15 shipping cutoff", k=3, min_score=0.05)
        assert hits
        assert any(hit.source == "holiday_memo.md" for hit in hits)
        assert store.ingest_text("Holiday Fulfillment Memo", "holiday_memo.md", text) == 0
        assert store.delete_doc(hits[0].doc_id) >= 1
    finally:
        engine.dispose()


def test_knowledge_search_tool(knowledge_store, rag_session_factory, tool_ctx):
    from app.rag.store import reset_knowledge_store

    reset_knowledge_store()
    tool_ctx.db_session_factory = rag_session_factory
    try:
        result = _execute(
            "knowledge_search",
            {"query": "why are Solstice chair returns increasing", "k": 3},
            tool_ctx,
        )
        assert result.ok, result.error
        assert result.output["count"] >= 1
        first = result.output["results"][0]
        assert {"chunk_id", "doc_id", "title", "source", "text", "score"} <= set(first)
        assert first["score"] > 0
        assert result.output["query"] == "why are Solstice chair returns increasing"
    finally:
        reset_knowledge_store()


def test_knowledge_search_tool_caps_agent_min_score(knowledge_store, rag_session_factory, tool_ctx):
    """A strict agent-supplied threshold must not silently empty the result set."""
    from app.rag.store import reset_knowledge_store

    reset_knowledge_store()
    tool_ctx.db_session_factory = rag_session_factory
    try:
        capped = _execute(
            "knowledge_search",
            {"query": "Solstice chair quality defect returns", "k": 3, "min_score": 0.5},
            tool_ctx,
        )
        assert capped.ok, capped.error
        assert capped.output["requested_min_score"] == 0.5
        assert capped.output["min_score"] <= 0.25
        assert capped.output["min_score_capped"] is True
        assert capped.output["count"] >= 1, "score cap should prevent an empty result set"

        defaulted = _execute(
            "knowledge_search",
            {"query": "Solstice chair quality defect returns", "k": 3},
            tool_ctx,
        )
        assert defaulted.ok
        assert defaulted.output["min_score"] >= 0.08  # config floor (RAG_MIN_SCORE)
        assert defaulted.output["requested_min_score"] == defaulted.output["min_score"]
        assert defaulted.output["min_score_capped"] is False

        low = _execute(
            "knowledge_search",
            {"query": "Solstice chair quality defect returns", "k": 3, "min_score": 0.0},
            tool_ctx,
        )
        assert low.ok
        assert low.output["min_score"] >= 0.08  # cannot go below the configured floor
    finally:
        reset_knowledge_store()


def test_knowledge_search_tool_handles_empty_store(rag_session_factory, tool_ctx, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.rag.store import reset_knowledge_store

    engine = create_engine(f"sqlite:///{(tmp_path / 'empty2.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    reset_knowledge_store()
    tool_ctx.db_session_factory = factory
    try:
        result = _execute("knowledge_search", {"query": "anything"}, tool_ctx)
        assert result.ok
        assert result.output["results"] == []
        assert "ingest" in result.output["note"].lower()
    finally:
        reset_knowledge_store()
        engine.dispose()


# ---------------------------------------------------------------------------
# E. web_search (offline-safe)
# ---------------------------------------------------------------------------

def test_web_search_is_offline_and_simulated(knowledge_dir, tool_ctx):
    tool_ctx.extras["market_dir"] = str(knowledge_dir / "market")
    result = _execute(
        "web_search", {"query": "ergonomic chair return rate benchmark", "max_results": 2}, tool_ctx
    )
    assert result.ok, result.error
    output = result.output
    assert output["simulated"] is True
    assert output["network"] is False
    assert output["count"] >= 1
    assert output["corpus_period"] == "2025-2026"
    assert output["as_of"] == "2026-10"
    first = output["results"][0]
    assert first["title"]
    assert first["snippet"]
    assert first["url"].startswith("https://")
    assert first["simulated"] is True
    assert first["period"] == "2025-2026"
    # Date consistency + product-agnostic wording: the simulated corpus must never let
    # an agent cite a 2024 benchmark or map a demo SKU onto another product category.
    returned_text = " ".join(
        f"{item['title']} {item['snippet']}" for item in output["results"]
    ).lower()
    assert "2024" not in returned_text
    for internal in ("solstice", "cirrus", "tide", "ns-2602", "blender", "kettle", "home appliance"):
        assert internal not in returned_text


def test_web_search_missing_corpus_never_hangs(tool_ctx, tmp_path):
    tool_ctx.extras["market_dir"] = str(tmp_path / "no-market-notes")
    result = _execute("web_search", {"query": "anything at all"}, tool_ctx)
    assert result.ok
    assert result.output["results"] == []
    assert result.output["simulated"] is True
    assert "no live web access" in result.output["note"].lower()


# ---------------------------------------------------------------------------
# F. report_writer
# ---------------------------------------------------------------------------

def test_report_writer_writes_markdown_artifact(tool_ctx, tmp_path):
    result = _execute(
        "report_writer",
        {
            "title": "Sales Underperformance Analysis",
            "content": "## Findings\n\n- Solstice: quality defect\n- Cirrus: discount dependency\n",
            "description": "Unit-test artifact",
            "name": "sales-analysis",
        },
        tool_ctx,
    )
    assert result.ok, result.error
    output = result.output
    artifact_path = Path(output["path"])
    assert artifact_path.is_file()
    assert artifact_path.parent == (tmp_path / "artifacts").resolve()
    assert artifact_path.suffix == ".md"
    text = artifact_path.read_text(encoding="utf-8")
    assert text.startswith("# Sales Underperformance Analysis")
    assert "quality defect" in text
    assert output["kind"] == "markdown"
    assert output["chars"] == len(text)
    assert output["bytes"] > 0


def test_report_writer_sanitizes_names_and_avoids_overwrite(tool_ctx, tmp_path):
    malicious = _execute(
        "report_writer",
        {"title": "Traversal", "content": "body", "name": "../../escape.md"},
        tool_ctx,
    )
    assert malicious.ok, malicious.error
    escaped_path = Path(malicious.output["path"])
    assert escaped_path.parent == (tmp_path / "artifacts").resolve()
    assert escaped_path.name == "escape.md"
    assert not (tmp_path / "escape.md").exists()

    first = _execute("report_writer", {"title": "Same Title", "content": "one"}, tool_ctx)
    second = _execute("report_writer", {"title": "Same Title", "content": "two"}, tool_ctx)
    assert first.ok and second.ok
    assert first.output["name"] != second.output["name"]
    assert Path(second.output["path"]).read_text(encoding="utf-8").strip().endswith("two")

    missing_content = _execute("report_writer", {"title": "No Content"}, tool_ctx)
    assert missing_content.ok is False
