# Metric Definitions & Canonical Queries

**Owner:** Analytics · **Updated:** 2026-09-25 · **Audience:** analysts and automated agents

Use these definitions consistently. The demo sales database is SQLite at `data/demo/sales.db`;
the same SQL (minus date functions) runs on the production warehouse.

## 1. Tables and grain

| Table | Grain |
|---|---|
| `products` | one row per active SKU |
| `sales` | one row per product × region × channel transaction day |
| `marketing_spend` | one row per campaign × product × month (`month` is `YYYY-MM`) |
| `support_tickets` | one row per ticket (`opened_at` ISO-8601 timestamp) |

## 2. Core definitions

| Metric | Formula |
|---|---|
| Units | `SUM(sales.quantity)` |
| Revenue | `SUM(sales.revenue)` — gross, after discount, before return credit |
| COGS | `SUM(sales.cost)` |
| Gross profit | revenue − cost |
| Gross margin % | gross profit / revenue |
| Discount rate | `1 − SUM(revenue) / SUM(quantity × unit_price)` |
| Return rate | returned units / units sold |
| Ticket rate | tickets / units × 1,000 |
| TTM | trailing 12 months of `sale_date` |

## 3. Canonical SQL — bottom SKUs by trailing-12-month revenue

```sql
SELECT p.product_id, p.name, ROUND(SUM(s.revenue), 2) AS revenue
FROM sales s
JOIN products p ON p.product_id = s.product_id
WHERE s.sale_date >= date('now', '-12 months')
GROUP BY p.product_id, p.name
ORDER BY revenue ASC
LIMIT 3;
```

For a fixed reporting window (preferred for reproducibility), replace the `WHERE` clause with
explicit bounds such as `s.sale_date BETWEEN '2025-11-01' AND '2026-10-31'`.

## 4. Canonical SQL — monthly margin and discount by SKU

```sql
SELECT p.name,
       substr(s.sale_date, 1, 7) AS month,
       ROUND(SUM(s.revenue), 2) AS revenue,
       ROUND(SUM(s.revenue - s.cost), 2) AS gross_profit,
       ROUND(SUM(s.revenue - s.cost) / SUM(s.revenue), 4) AS gross_margin,
       ROUND(1 - SUM(s.revenue) / SUM(s.quantity * p.unit_price), 4) AS discount_rate
FROM sales s
JOIN products p ON p.product_id = s.product_id
GROUP BY p.name, month
ORDER BY p.name, month;
```

## 5. Canonical SQL — return and ticket rates by SKU

```sql
SELECT p.name,
       ROUND(SUM(CASE WHEN s.returned = 1 THEN s.quantity ELSE 0 END) * 1.0
             / SUM(s.quantity), 4) AS return_rate
FROM sales s
JOIN products p ON p.product_id = s.product_id
GROUP BY p.name
ORDER BY return_rate DESC;
```

```sql
SELECT p.name, COUNT(*) AS tickets
FROM support_tickets t
JOIN products p ON p.product_id = t.product_id
GROUP BY p.name
ORDER BY tickets DESC;
```

## 6. What counts as underperformance

A SKU is underperforming if it is in the bottom three by TTM revenue **or** its revenue is declining
year over year while margin or return rate deteriorates. Root-cause analysis compares, at minimum:
pricing/discount trend, return rate, support-ticket rate and marketing spend. A SKU can have normal
quality and still be a commercial failure (discount dependency), or normal pricing and fail on quality.

## 7. Caveats

- `date('now')` depends on the runtime clock; use explicit date ranges for reports.
- Revenue does not net returns. Report return rate alongside revenue.
- Marketing spend is per product-month; it is not attributed to orders.
- Ticket counts must be normalized per 1,000 units when volumes change.
