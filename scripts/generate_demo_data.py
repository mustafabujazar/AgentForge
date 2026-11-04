#!/usr/bin/env python
"""Generate the deterministic AgentForge demo dataset and knowledge corpus.

Outputs
-------
``data/demo/sales.db``                     SQLite database with the four tables
                                           defined in docs/ARCHITECTURE.md §6.
``data/demo/products.csv``                 Product catalog (CSV mirror).
``data/demo/monthly_product_metrics.csv``  Product-month aggregates for the csv tool.
``data/demo/region_month_metrics.csv``     Region-month aggregates for the csv tool.
``data/knowledge/*.md``                    Internal memos (9) plus market notes (4).

The dataset covers 18 months of daily sales (2025-05-01 → 2026-10-31) for eight
products across four regions. Three products are deliberately underperforming,
each with a *different* causal story:

* ``Solstice Ergonomic Chair``    — supplier quality defect: returns + support
  tickets rise from month 7, revenue declines while price and marketing stay flat.
* ``Cirrus Air Purifier``         — price/discount dependency: discount depth ramps
  ~5% → ~35%, gross margin collapses while unit volume is only held up by promotions.
* ``Tide Portable Speaker``       — weak marketing: marketing spend far below the
  viability floor with stable pricing/quality, units decline and revenue is lowest.

The generator is **deterministic** (fixed seed, fixed iteration order, separate RNG
streams) and **idempotent** (recreates the DB file and rewrites the corpus in place;
running twice produces identical content).

Usage::

    .\\venv\\Scripts\\python.exe scripts/generate_demo_data.py
    .\\venv\\Scripts\\python.exe scripts/generate_demo_data.py --seed 20260101 --quiet

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

# ``demo_knowledge`` lives next to this script. Support both direct execution
# (``python scripts/generate_demo_data.py``) and package import
# (``from scripts.generate_demo_data import generate``).
_SCRIPT_DIR = Path(__file__).resolve().parent
if __package__:
    from scripts.demo_knowledge import all_docs as _all_knowledge_docs
else:  # pragma: no cover - exercised by direct CLI execution
    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    from demo_knowledge import all_docs as _all_knowledge_docs


# ---------------------------------------------------------------------------
# Dataset constants
# ---------------------------------------------------------------------------

DEFAULT_SEED = 20260101
START_DATE = date(2025, 5, 1)
END_DATE = date(2026, 10, 31)

REGIONS: tuple[str, ...] = ("North", "South", "East", "West")
REGION_MULTIPLIERS: dict[str, float] = {
    "North": 1.15,
    "South": 0.90,
    "East": 1.00,
    "West": 0.95,
}
CHANNELS: tuple[str, ...] = ("online", "retail", "partner")
CHANNEL_WEIGHTS: tuple[float, ...] = (0.60, 0.27, 0.13)

# Month-of-year demand multiplier (Q4 peak, January trough).
SEASONALITY: dict[int, float] = {
    1: 0.90,
    2: 0.88,
    3: 0.96,
    4: 0.97,
    5: 1.00,
    6: 0.99,
    7: 1.01,
    8: 1.03,
    9: 1.05,
    10: 1.08,
    11: 1.18,
    12: 1.26,
}

ISSUE_TYPES: dict[str, tuple[str, ...]] = {
    "lamp": ("setup", "usability", "electrical", "noise", "shipping_damage", "missing_parts"),
    "headphones": ("connectivity", "battery", "firmware", "noise", "usability", "shipping_damage"),
    "desk": ("setup", "usability", "shipping_damage", "missing_parts"),
    "espresso": ("setup", "usability", "noise", "electrical", "shipping_damage"),
    "bulb": ("connectivity", "setup", "firmware", "usability", "shipping_damage"),
    "chair": ("usability", "mechanism_noise", "shipping_damage", "missing_parts", "defect"),
    "purifier": ("noise", "setup", "usability", "firmware", "shipping_damage"),
    "speaker": ("connectivity", "battery", "firmware", "noise", "usability", "shipping_damage"),
}


@dataclass(frozen=True)
class Product:
    product_id: int
    name: str
    category: str
    unit_price: float
    unit_cost: float
    launch_date: str
    monthly_units: float
    profile: str  # healthy | quality_defect | discount_dependency | weak_marketing
    discount_base: float
    marketing_base: float
    growth: float
    issue_pool: str


PRODUCTS: tuple[Product, ...] = (
    Product(1, "Aurora Arc Desk Lamp", "Lighting", 89.00, 38.50, "2023-03-15", 700, "healthy", 0.06, 5500.0, 0.006, "lamp"),
    Product(2, "Nimbus Wireless Headphones", "Audio", 149.00, 62.00, "2023-09-05", 900, "healthy", 0.05, 12000.0, 0.008, "headphones"),
    Product(3, "Terra Bamboo Standing Desk", "Furniture", 449.00, 238.00, "2024-01-22", 260, "healthy", 0.05, 9000.0, 0.004, "desk"),
    Product(4, "Vela Espresso Machine", "Kitchen", 329.00, 148.00, "2024-04-10", 300, "healthy", 0.06, 7000.0, 0.005, "espresso"),
    Product(5, "Lumen Smart Bulb 4-Pack", "Lighting", 49.99, 19.50, "2022-11-01", 1000, "healthy", 0.08, 4500.0, 0.005, "bulb"),
    Product(6, "Solstice Ergonomic Chair", "Furniture", 399.00, 190.00, "2024-08-19", 120, "quality_defect", 0.04, 6000.0, 0.0, "chair"),
    Product(7, "Cirrus Air Purifier", "Kitchen", 249.00, 90.00, "2024-02-14", 150, "discount_dependency", 0.05, 16000.0, 0.0, "purifier"),
    Product(8, "Tide Portable Speaker", "Audio", 79.99, 34.00, "2022-06-30", 180, "weak_marketing", 0.06, 1200.0, 0.0, "speaker"),
)

PRODUCTS_BY_ID: dict[int, Product] = {p.product_id: p for p in PRODUCTS}

SEVERITY_WEIGHTS: dict[str, tuple[tuple[str, float], ...]] = {
    "normal": (("low", 0.45), ("medium", 0.38), ("high", 0.14), ("critical", 0.03)),
    "defect_post": (("low", 0.08), ("medium", 0.30), ("high", 0.42), ("critical", 0.20)),
}

DDL = """
CREATE TABLE products (
    product_id  INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    unit_price  REAL    NOT NULL,
    unit_cost   REAL    NOT NULL,
    launch_date TEXT    NOT NULL
);

CREATE TABLE sales (
    sale_id    INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    region     TEXT    NOT NULL,
    channel    TEXT    NOT NULL,
    quantity   INTEGER NOT NULL,
    revenue    REAL    NOT NULL,
    cost       REAL    NOT NULL,
    discount   REAL    NOT NULL DEFAULT 0,
    returned   INTEGER NOT NULL DEFAULT 0,
    sale_date  TEXT    NOT NULL
);

CREATE TABLE marketing_spend (
    campaign_id TEXT PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(product_id),
    month       TEXT    NOT NULL,
    spend       REAL    NOT NULL
);

CREATE TABLE support_tickets (
    ticket_id  INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    issue_type TEXT    NOT NULL,
    severity   TEXT    NOT NULL,
    opened_at  TEXT    NOT NULL
);

CREATE INDEX idx_sales_product_date ON sales(product_id, sale_date);
CREATE INDEX idx_sales_date ON sales(sale_date);
CREATE INDEX idx_sales_region ON sales(region);
CREATE INDEX idx_marketing_product_month ON marketing_spend(product_id, month);
CREATE INDEX idx_tickets_product_date ON support_tickets(product_id, opened_at);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def month_index(value: date) -> int:
    """Zero-based month offset from the first data month."""
    return (value.year - START_DATE.year) * 12 + (value.month - START_DATE.month)


def month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - date(year, month, 1)).days


def iter_months(start: date, end: date) -> Iterable[date]:
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        yield cursor
        cursor = date(cursor.year + (cursor.month // 12), (cursor.month % 12) + 1, 1)


def _poisson(rng: random.Random, lam: float) -> int:
    """Small deterministic Poisson sampler (Knuth); normal approximation above 30."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= limit:
            return k - 1


def _weighted_choice(rng: random.Random, options: tuple[str, ...], weights: tuple[float, ...]) -> str:
    roll = rng.random()
    cumulative = 0.0
    for option, weight in zip(options, weights):
        cumulative += weight
        if roll <= cumulative:
            return option
    return options[-1]


def _weighted_choice_pairs(rng: random.Random, pairs: Iterable[tuple[str, float]]) -> str:
    pairs = list(pairs)
    return _weighted_choice(rng, tuple(p for p, _ in pairs), tuple(w for _, w in pairs))


# ---------------------------------------------------------------------------
# Story mechanics (one place to tune each deliberate narrative)
# ---------------------------------------------------------------------------

def trend_factor(product: Product, mi: int) -> float:
    """Monthly unit-demand trend for a product."""
    if product.profile == "quality_defect":
        # Stable until the defective batch reaches customers (month 6), then decline.
        return 1.0 if mi < 6 else 0.92 ** (mi - 6)
    if product.profile == "discount_dependency":
        # Units only creep up because promotions are running.
        return 1.0 + 0.004 * mi
    if product.profile == "weak_marketing":
        return 0.985 ** mi
    return (1.0 + product.growth) ** mi


def discount_for(product: Product, mi: int, rng: random.Random) -> float:
    """Effective discount for one sale line."""
    if product.profile == "discount_dependency":
        progress = min(1.0, max(0.0, (mi - 3) / 9.0))
        base = 0.05 + 0.30 * progress  # ~5% -> ~35% over nine months
    elif product.profile == "quality_defect":
        base = 0.04 if mi < 6 else 0.08
    else:
        base = product.discount_base
    jitter = rng.uniform(-0.012, 0.012)
    return round(max(0.0, min(0.45, base + jitter)), 4)


def return_probability(product: Product, mi: int) -> float:
    """Per-sale-line (whole-line) return probability."""
    if product.profile == "quality_defect":
        if mi < 6:
            return 0.03
        return min(0.22, 0.03 + 0.018 * (mi - 5))
    if product.profile == "discount_dependency":
        progress = min(1.0, max(0.0, (mi - 3) / 9.0))
        return 0.05 + 0.02 * progress
    return 0.03


def marketing_spend_for(product: Product, mi: int, month: date, rng: random.Random) -> float:
    """Monthly marketing spend per product."""
    seasonal = 1.35 if month.month in (11, 12) else (0.92 if month.month in (1, 2) else 1.0)
    if product.profile == "quality_defect":
        base = product.marketing_base  # deliberately flat: not a marketing problem
    elif product.profile == "discount_dependency":
        base = product.marketing_base + 120.0 * mi  # highest spend, promotion support
    elif product.profile == "weak_marketing":
        base = product.marketing_base * (1.0 - 0.012 * mi)  # below viability floor, drifting down
    else:
        base = product.marketing_base * (1.0 + 0.015 * mi) * seasonal
    spend = base * rng.uniform(0.94, 1.06)
    return round(spend, 2)


def ticket_rate(product: Product, mi: int) -> float:
    """Expected tickets per unit sold."""
    if product.profile == "quality_defect":
        if mi < 6:
            return 0.010
        return min(0.22, 0.06 + 0.015 * (mi - 6))
    if product.profile == "discount_dependency":
        return 0.018
    if product.profile == "weak_marketing":
        return 0.012
    return 0.008


def ticket_type_pool(product: Product, mi: int) -> tuple[str, ...]:
    if product.profile == "quality_defect" and mi >= 6:
        return ("defect", "defect", "mechanism_noise", "mechanism_noise", "usability", "missing_parts")
    return ISSUE_TYPES[product.issue_pool]


def ticket_severity(product: Product, mi: int, rng: random.Random) -> str:
    weights = SEVERITY_WEIGHTS["defect_post"] if product.profile == "quality_defect" and mi >= 6 else SEVERITY_WEIGHTS["normal"]
    return _weighted_choice_pairs(rng, weights)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

@dataclass
class Dataset:
    products: list[dict[str, Any]]
    sales: list[tuple[Any, ...]]
    marketing: list[tuple[Any, ...]]
    tickets: list[tuple[Any, ...]]


def _generate_products() -> list[dict[str, Any]]:
    return [
        {
            "product_id": p.product_id,
            "name": p.name,
            "category": p.category,
            "unit_price": p.unit_price,
            "unit_cost": p.unit_cost,
            "launch_date": p.launch_date,
        }
        for p in PRODUCTS
    ]


def _generate_sales(seed: int) -> list[tuple[Any, ...]]:
    """Daily product × region sales rows. Rows with zero demand are omitted."""
    rng = random.Random(seed)
    rows: list[tuple[Any, ...]] = []
    sale_id = 0
    day = START_DATE
    while day <= END_DATE:
        mi = month_index(day)
        season = SEASONALITY[day.month]
        dim = days_in_month(day.year, day.month)
        for product in PRODUCTS:
            monthly_target = product.monthly_units * trend_factor(product, mi) * season
            for region in REGIONS:
                lam = (monthly_target / 4.0 / dim) * REGION_MULTIPLIERS[region]
                quantity = _poisson(rng, lam)
                if quantity <= 0:
                    continue
                discount = discount_for(product, mi, rng)
                channel = _weighted_choice(rng, CHANNELS, CHANNEL_WEIGHTS)
                revenue = round(quantity * product.unit_price * (1.0 - discount), 2)
                cost = round(quantity * product.unit_cost, 2)
                returned = 1 if rng.random() < return_probability(product, mi) else 0
                sale_id += 1
                rows.append(
                    (
                        sale_id,
                        product.product_id,
                        region,
                        channel,
                        quantity,
                        revenue,
                        cost,
                        discount,
                        returned,
                        day.isoformat(),
                    )
                )
        day += timedelta(days=1)
    return rows


def _generate_marketing(seed: int) -> list[tuple[Any, ...]]:
    rng = random.Random(seed + 1)
    rows: list[tuple[Any, ...]] = []
    campaign = 0
    for month in iter_months(START_DATE, END_DATE):
        mi = month_index(month)
        for product in PRODUCTS:
            campaign += 1
            spend = marketing_spend_for(product, mi, month, rng)
            rows.append((f"CMP-{campaign:04d}", product.product_id, month_key(month), spend))
    return rows


def _generate_tickets(seed: int, sales: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Support tickets derived from monthly units, with the chair defect spike."""
    rng = random.Random(seed + 2)
    units_by_product_month: dict[tuple[int, str], int] = {}
    for row in sales:
        product_id, quantity, sale_date = row[1], row[4], row[9]
        key = (product_id, sale_date[:7])
        units_by_product_month[key] = units_by_product_month.get(key, 0) + quantity

    rows: list[tuple[Any, ...]] = []
    ticket_id = 0
    for month in iter_months(START_DATE, END_DATE):
        mi = month_index(month)
        dim = days_in_month(month.year, month.month)
        for product in PRODUCTS:
            units = units_by_product_month.get((product.product_id, month_key(month)), 0)
            if units <= 0:
                continue
            count = _poisson(rng, units * ticket_rate(product, mi))
            pool = ticket_type_pool(product, mi)
            for _ in range(count):
                issue_type = pool[rng.randrange(len(pool))]
                severity = ticket_severity(product, mi, rng)
                opened_at = (
                    f"{month_key(month)}-{rng.randint(1, dim):02d}T"
                    f"{rng.randint(8, 19):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
                )
                ticket_id += 1
                rows.append((ticket_id, product.product_id, issue_type, severity, opened_at))
    return rows


def build_dataset(seed: int = DEFAULT_SEED) -> Dataset:
    sales = _generate_sales(seed)
    return Dataset(
        products=_generate_products(),
        sales=sales,
        marketing=_generate_marketing(seed),
        tickets=_generate_tickets(seed, sales),
    )


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _write_sqlite(db_path: Path, dataset: Dataset) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(DDL)
        connection.executemany(
            "INSERT INTO products (product_id, name, category, unit_price, unit_cost, launch_date)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (p["product_id"], p["name"], p["category"], p["unit_price"], p["unit_cost"], p["launch_date"])
                for p in dataset.products
            ],
        )
        connection.executemany(
            "INSERT INTO sales (sale_id, product_id, region, channel, quantity, revenue, cost,"
            " discount, returned, sale_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            dataset.sales,
        )
        connection.executemany(
            "INSERT INTO marketing_spend (campaign_id, product_id, month, spend) VALUES (?, ?, ?, ?)",
            dataset.marketing,
        )
        connection.executemany(
            "INSERT INTO support_tickets (ticket_id, product_id, issue_type, severity, opened_at)"
            " VALUES (?, ?, ?, ?, ?)",
            dataset.tickets,
        )
        connection.commit()
    finally:
        connection.close()


def _aggregate_monthly(dataset: Dataset) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Product-month and region-month aggregates used for the CSV mirrors."""
    marketing_by_key = {(row[1], row[2]): float(row[3]) for row in dataset.marketing}
    product_month: dict[tuple[int, str], dict[str, Any]] = {}
    region_month: dict[tuple[str, str], dict[str, Any]] = {}
    for row in dataset.sales:
        _, product_id, region, _channel, quantity, revenue, cost, _discount, returned, sale_date = row
        month = sale_date[:7]
        product = PRODUCTS_BY_ID[product_id]

        pm = product_month.setdefault(
            (product_id, month),
            {
                "month": month,
                "product_id": product_id,
                "product_name": product.name,
                "category": product.category,
                "units": 0,
                "revenue": 0.0,
                "cost": 0.0,
                "returned_units": 0,
                "marketing_spend": marketing_by_key.get((product_id, month), 0.0),
            },
        )
        pm["units"] += quantity
        pm["revenue"] += revenue
        pm["cost"] += cost
        pm["returned_units"] += quantity if returned else 0

        rm = region_month.setdefault(
            (month, region),
            {"month": month, "region": region, "units": 0, "revenue": 0.0, "cost": 0.0},
        )
        rm["units"] += quantity
        rm["revenue"] += revenue
        rm["cost"] += cost

    product_rows: list[dict[str, Any]] = []
    for pm in product_month.values():
        product = PRODUCTS_BY_ID[pm["product_id"]]
        gross_profit = round(pm["revenue"] - pm["cost"], 2)
        units = pm["units"]
        product_rows.append(
            {
                **pm,
                "revenue": round(pm["revenue"], 2),
                "cost": round(pm["cost"], 2),
                "gross_profit": gross_profit,
                "gross_margin": round(gross_profit / pm["revenue"], 4) if pm["revenue"] else 0.0,
                "discount_rate": round(1.0 - pm["revenue"] / (units * product.unit_price), 4) if units else 0.0,
                "return_rate": round(pm["returned_units"] / units, 4) if units else 0.0,
            }
        )
    product_rows.sort(key=lambda item: (item["month"], item["product_id"]))

    region_rows: list[dict[str, Any]] = []
    for rm in region_month.values():
        gross_profit = round(rm["revenue"] - rm["cost"], 2)
        region_rows.append(
            {
                **rm,
                "revenue": round(rm["revenue"], 2),
                "cost": round(rm["cost"], 2),
                "gross_profit": gross_profit,
                "gross_margin": round(gross_profit / rm["revenue"], 4) if rm["revenue"] else 0.0,
            }
        )
    region_rows.sort(key=lambda item: (item["month"], item["region"]))
    return product_rows, region_rows


def _write_csvs(demo_dir: Path, dataset: Dataset) -> dict[str, Path]:
    demo_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    products_path = demo_dir / "products.csv"
    with products_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["product_id", "name", "category", "unit_price", "unit_cost", "launch_date"],
        )
        writer.writeheader()
        writer.writerows(dataset.products)
    paths["products"] = products_path

    product_rows, region_rows = _aggregate_monthly(dataset)

    product_path = demo_dir / "monthly_product_metrics.csv"
    product_fields = [
        "month",
        "product_id",
        "product_name",
        "category",
        "units",
        "revenue",
        "cost",
        "gross_profit",
        "gross_margin",
        "discount_rate",
        "returned_units",
        "return_rate",
        "marketing_spend",
    ]
    with product_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=product_fields)
        writer.writeheader()
        writer.writerows(product_rows)
    paths["monthly_product_metrics"] = product_path

    region_path = demo_dir / "region_month_metrics.csv"
    region_fields = ["month", "region", "units", "revenue", "cost", "gross_profit", "gross_margin"]
    with region_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=region_fields)
        writer.writeheader()
        writer.writerows(region_rows)
    paths["region_month_metrics"] = region_path

    return paths


def _write_knowledge(root: Path) -> list[Path]:
    knowledge_dir = root / "data" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    # Remove previously generated markdown only (do not touch anything else).
    for stale in sorted(knowledge_dir.rglob("*.md")):
        stale.unlink()
    written: list[Path] = []
    for rel_path, text in _all_knowledge_docs():
        destination = knowledge_dir / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8", newline="\n")
        written.append(destination)
    return written


# ---------------------------------------------------------------------------
# Summary / evidence
# ---------------------------------------------------------------------------

def _product_stats(dataset: Dataset) -> dict[int, dict[str, Any]]:
    stats: dict[int, dict[str, Any]] = {
        p.product_id: {
            "product_id": p.product_id,
            "name": p.name,
            "profile": p.profile,
            "units": 0,
            "revenue": 0.0,
            "cost": 0.0,
            "returned_units": 0,
            "ttm_units": 0,
            "ttm_revenue": 0.0,
            "marketing_total": 0.0,
            "marketing_months": 0,
            "ttm_marketing": 0.0,
        }
        for p in PRODUCTS
    }
    ttm_start = date(END_DATE.year - 1, END_DATE.month + 1, 1) if END_DATE.month < 12 else date(END_DATE.year, 1, 1)
    for row in dataset.sales:
        _, product_id, _region, _channel, quantity, revenue, _cost, _discount, returned, sale_date = row
        stat = stats[product_id]
        stat["units"] += quantity
        stat["revenue"] += revenue
        stat["returned_units"] += quantity if returned else 0
        if sale_date >= ttm_start.isoformat():
            stat["ttm_units"] += quantity
            stat["ttm_revenue"] += revenue
    for row in dataset.marketing:
        _, product_id, month, spend = row
        stat = stats[product_id]
        stat["marketing_total"] += spend
        stat["marketing_months"] += 1
        if month >= ttm_start.strftime("%Y-%m"):
            stat["ttm_marketing"] += spend
    for stat in stats.values():
        stat["revenue"] = round(stat["revenue"], 2)
        stat["ttm_revenue"] = round(stat["ttm_revenue"], 2)
        stat["return_rate"] = round(stat["returned_units"] / stat["units"], 4) if stat["units"] else 0.0
        stat["avg_marketing"] = round(stat["marketing_total"] / stat["marketing_months"], 2) if stat["marketing_months"] else 0.0
        stat["avg_ttm_marketing"] = round(stat["ttm_marketing"] / 12.0, 2)
    return stats


def _period_metrics(
    dataset: Dataset,
    start: str,
    end: str,
    product_id: int | None = None,
) -> dict[str, float]:
    """Unit-weighted margin/discount/return metrics over a sale_date window."""
    units = 0
    revenue = 0.0
    cost = 0.0
    returned_units = 0
    for row in dataset.sales:
        if product_id is not None and row[1] != product_id:
            continue
        sale_date = row[9]
        if not (start <= sale_date <= end):
            continue
        units += row[4]
        revenue += row[5]
        cost += row[6]
        returned_units += row[4] if row[8] else 0
    gross_profit = revenue - cost
    discount_rate = 0.0
    if units and product_id is not None:
        list_price = PRODUCTS_BY_ID[product_id].unit_price
        discount_rate = 1.0 - revenue / (units * list_price)
    return {
        "units": float(units),
        "revenue": round(revenue, 2),
        "gross_margin": round(gross_profit / revenue, 4) if revenue else 0.0,
        "return_rate": round(returned_units / units, 4) if units else 0.0,
        "avg_price": round(revenue / units, 2) if units else 0.0,
        "discount_rate": round(discount_rate, 4),
    }


def build_summary(dataset: Dataset, db_path: Path, knowledge_files: list[Path], seed: int) -> dict[str, Any]:
    stats = _product_stats(dataset)
    ordered = sorted(stats.values(), key=lambda item: item["ttm_revenue"])
    bottom3 = ordered[:3]
    ttm_start = date(END_DATE.year - 1, END_DATE.month + 1, 1)

    chair = stats[6]
    cirrus = stats[7]
    tide = stats[8]
    # Same-season, year-over-year windows: the defect becomes visible from 2025-11.
    baseline_start, baseline_end = "2025-05-01", "2025-10-31"
    latest_start, latest_end = "2026-05-01", END_DATE.isoformat()
    chair_baseline = _period_metrics(dataset, baseline_start, baseline_end, 6)
    chair_latest = _period_metrics(dataset, latest_start, latest_end, 6)
    cirrus_early = _period_metrics(dataset, "2025-05-01", "2025-08-31", 7)
    cirrus_late = _period_metrics(dataset, "2026-07-01", END_DATE.isoformat(), 7)
    tide_baseline = _period_metrics(dataset, baseline_start, baseline_end, 8)
    tide_latest = _period_metrics(dataset, latest_start, latest_end, 8)

    def _tickets(product_id: int, start: str, end: str) -> int:
        return sum(
            1
            for row in dataset.tickets
            if row[1] == product_id and start <= row[4][:10] <= end
        )

    def _change(before: float, after: float) -> float:
        return round((after - before) / before, 4) if before else 0.0

    months_baseline = 6
    months_latest = 6
    chair_tickets_baseline = _tickets(6, baseline_start, baseline_end)
    chair_tickets_latest = _tickets(6, latest_start, latest_end)

    tickets_by_product: dict[int, int] = {}
    for row in dataset.tickets:
        tickets_by_product[row[1]] = tickets_by_product.get(row[1], 0) + 1

    summary: dict[str, Any] = {
        "seed": seed,
        "db_path": str(db_path),
        "knowledge_dir": str(db_path.parents[1] / "knowledge"),
        "knowledge_files": [str(p) for p in knowledge_files],
        "row_counts": {
            "products": len(dataset.products),
            "sales": len(dataset.sales),
            "marketing_spend": len(dataset.marketing),
            "support_tickets": len(dataset.tickets),
        },
        "date_range": {"start": START_DATE.isoformat(), "end": END_DATE.isoformat()},
        "ttm_window": {"start": ttm_start.isoformat(), "end": END_DATE.isoformat()},
        "total_revenue": round(sum(s["revenue"] for s in stats.values()), 2),
        "total_units": sum(s["units"] for s in stats.values()),
        "products": [
            {
                "product_id": s["product_id"],
                "name": s["name"],
                "profile": s["profile"],
                "units": s["units"],
                "revenue": s["revenue"],
                "ttm_revenue": s["ttm_revenue"],
                "return_rate": s["return_rate"],
                "avg_marketing": s["avg_marketing"],
                "tickets": tickets_by_product.get(s["product_id"], 0),
            }
            for s in sorted(stats.values(), key=lambda item: item["product_id"])
        ],
        "bottom3_ttm": [
            {
                "rank": index + 1,
                "product_id": item["product_id"],
                "name": item["name"],
                "ttm_revenue": item["ttm_revenue"],
                "return_rate": item["return_rate"],
            }
            for index, item in enumerate(bottom3)
        ],
        "story_evidence": {
            "quality_defect": {
                "product": chair["name"],
                "ttm_revenue": chair["ttm_revenue"],
                "return_rate_baseline": chair_baseline["return_rate"],
                "return_rate_latest": chair_latest["return_rate"],
                "tickets_per_month_baseline": round(chair_tickets_baseline / months_baseline, 2),
                "tickets_per_month_latest": round(chair_tickets_latest / months_latest, 2),
                "revenue_baseline_6m": chair_baseline["revenue"],
                "revenue_latest_6m": chair_latest["revenue"],
                "revenue_change_pct": _change(chair_baseline["revenue"], chair_latest["revenue"]),
                "discount_rate_latest": chair_latest["discount_rate"],
                "marketing_avg_ttm": round(chair["avg_ttm_marketing"], 2),
            },
            "discount_dependency": {
                "product": cirrus["name"],
                "ttm_revenue": cirrus["ttm_revenue"],
                "discount_rate_early": cirrus_early["discount_rate"],
                "discount_rate_late": cirrus_late["discount_rate"],
                "gross_margin_early": cirrus_early["gross_margin"],
                "gross_margin_late": cirrus_late["gross_margin"],
                "return_rate_latest": cirrus_late["return_rate"],
                "units_early_4m": cirrus_early["units"],
                "units_late_4m": cirrus_late["units"],
                "marketing_avg_ttm": round(cirrus["avg_ttm_marketing"], 2),
            },
            "weak_marketing": {
                "product": tide["name"],
                "ttm_revenue": tide["ttm_revenue"],
                "marketing_avg_ttm": round(tide["avg_ttm_marketing"], 2),
                "units_baseline_6m": tide_baseline["units"],
                "units_latest_6m": tide_latest["units"],
                "units_change_pct": _change(tide_baseline["units"], tide_latest["units"]),
                "revenue_baseline_6m": tide_baseline["revenue"],
                "revenue_latest_6m": tide_latest["revenue"],
                "revenue_change_pct": _change(tide_baseline["revenue"], tide_latest["revenue"]),
                "discount_rate_baseline": tide_baseline["discount_rate"],
                "discount_rate_latest": tide_latest["discount_rate"],
                "return_rate_latest": tide_latest["return_rate"],
            },
        },
    }
    return summary


def print_summary(dataset: Dataset, summary: dict[str, Any]) -> None:
    print("AgentForge demo dataset generated")
    print("=" * 78)
    print(f"seed={summary['seed']}  window={summary['date_range']['start']} -> {summary['date_range']['end']}")
    print(f"db={summary['db_path']}")
    print(
        "rows: "
        + ", ".join(f"{name}={count:,}" for name, count in summary["row_counts"].items())
    )
    print(
        f"knowledge: {len(summary['knowledge_files'])} files in {summary['knowledge_dir']}"
    )
    print(
        f"total revenue: ${summary['total_revenue']:,.2f}   units: {summary['total_units']:,}"
    )
    print()
    print(f"{'SKU':38s} {'profile':20s} {'TTM rev':>12s} {'units':>8s} {'returns':>8s} {'mktg/mo':>9s}")
    print("-" * 78)
    for item in summary["products"]:
        print(
            f"{item['name'][:37]:38s} {item['profile']:20s} "
            f"{item['ttm_revenue']:>12,.0f} {item['units']:>8,} "
            f"{item['return_rate'] * 100:>7.1f}% {item['avg_marketing']:>9,.0f}"
        )
    print()
    print("Bottom 3 by trailing-12-month revenue (the demo objective):")
    for item in summary["bottom3_ttm"]:
        print(f"  {item['rank']}. {item['name']:32s} ${item['ttm_revenue']:,.0f}  return rate {item['return_rate'] * 100:.1f}%")
    print()
    evidence = summary["story_evidence"]
    q = evidence["quality_defect"]
    print("Story 1 - quality defect:", q["product"])
    print(
        f"  return rate {q['return_rate_baseline'] * 100:.1f}% -> {q['return_rate_latest'] * 100:.1f}% | "
        f"tickets/mo {q['tickets_per_month_baseline']:.1f} -> {q['tickets_per_month_latest']:.1f} | "
        f"marketing unchanged ~${q['marketing_avg_ttm']:,.0f}/mo"
    )
    print(
        f"  revenue (same 6-month season) ${q['revenue_baseline_6m']:,.0f} -> "
        f"${q['revenue_latest_6m']:,.0f} ({q['revenue_change_pct'] * 100:+.1f}%)"
    )
    d = evidence["discount_dependency"]
    print("Story 2 - discount dependency:", d["product"])
    print(
        f"  discount {d['discount_rate_early'] * 100:.1f}% -> {d['discount_rate_late'] * 100:.1f}% | "
        f"gross margin {d['gross_margin_early'] * 100:.1f}% -> {d['gross_margin_late'] * 100:.1f}% | "
        f"marketing ~${d['marketing_avg_ttm']:,.0f}/mo"
    )
    print(
        f"  units (4-month window) {d['units_early_4m']:,.0f} -> {d['units_late_4m']:,.0f} | "
        f"return rate {d['return_rate_latest'] * 100:.1f}% (quality normal)"
    )
    w = evidence["weak_marketing"]
    print("Story 3 - weak marketing:", w["product"])
    print(
        f"  marketing ~${w['marketing_avg_ttm']:,.0f}/mo (floor $4,000) | "
        f"units (same 6-month season) {w['units_baseline_6m']:,.0f} -> {w['units_latest_6m']:,.0f} "
        f"({w['units_change_pct'] * 100:+.1f}%)"
    )
    print(
        f"  discount {w['discount_rate_baseline'] * 100:.1f}% -> {w['discount_rate_latest'] * 100:.1f}% "
        f"(stable) | revenue {w['revenue_change_pct'] * 100:+.1f}% | return rate {w['return_rate_latest'] * 100:.1f}%"
    )
    print()
    print("Done. No network, no pip installs, deterministic.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate(root: Path | str = ROOT, seed: int = DEFAULT_SEED, quiet: bool = False) -> dict[str, Any]:
    """Generate the full demo dataset under ``root`` and return a summary dict.

    Idempotent: existing generated files are overwritten. ``root`` must contain
    (or will get) a ``data/`` directory. This is the function tests call with a
    temporary directory.
    """
    root_path = Path(root)
    demo_dir = root_path / "data" / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)

    dataset = build_dataset(seed)
    db_path = demo_dir / "sales.db"
    _write_sqlite(db_path, dataset)
    _write_csvs(demo_dir, dataset)
    knowledge_files = _write_knowledge(root_path)
    summary = build_summary(dataset, db_path, knowledge_files, seed)
    if not quiet:
        print_summary(dataset, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the AgentForge demo sales dataset and knowledge corpus.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"random seed (default: {DEFAULT_SEED})")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: repository root)")
    parser.add_argument("--quiet", action="store_true", help="suppress the summary output")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    generate(root=args.root, seed=args.seed, quiet=args.quiet)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
