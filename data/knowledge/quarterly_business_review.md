# Q3 FY26 Quarterly Business Review — Hardware Portfolio

**Attendees:** CEO, VP Revenue, VP Product, Head of Growth, Head of Support
**Date:** 2026-10-15 · **Period covered:** 2025-11-01 to 2026-10-31 (trailing 12 months)

## 1. Executive summary

The portfolio is roughly flat year over year (+2% in the same six-month seasonal window). The five
healthy SKUs are growing about 4% and carry the business; the bottom three are a drag. Three SKUs
sit at the bottom of the portfolio, and each one has a *different* root cause:

1. **Solstice Ergonomic Chair — supplier quality defect.** Returns and support tickets spiked, then
   revenue fell. Price and marketing did not change.
2. **Cirrus Air Purifier — discount dependency.** Units are held up by promotions and gross margin
   has collapsed. Quality is fine.
3. **Tide Portable Speaker — weak marketing.** Demand generation spend is below the viability floor;
   traffic and conversion are down while pricing is stable and quality is normal.

The most important decision from this review: **do not apply a single lever — more discounting — to
all three.** Discounting would deepen Cirrus' dependency, would not fix Solstice's defect, and would
burn margin on Tide without creating demand.

## 2. Bottom three SKUs (trailing 12 months)

| Rank | SKU | TTM revenue | TTM gross margin | Return rate (TTM) | Tickets / 1k units (TTM) | Diagnosis |
|---|---|---|---|---|---|---|
| 8 | Tide Portable Speaker (AU-3019) | ~$0.14M | ~55% | ~3% | ~11 | weak marketing, spend below floor |
| 7 | Cirrus Air Purifier (AF-1007) | ~$0.34M | ~49% | ~7% | ~15 | discount dependency, margin erosion |
| 6 | Solstice Ergonomic Chair (FN-2042) | ~$0.38M | ~48% | ~12% | ~146 | supplier defect, returns + tickets |

For comparison, the same window shows roughly: Nimbus $1.73M, Terra $1.46M, Vela $1.20M,
Aurora $0.77M, Lumen $0.60M. The gap between the bottom three and the rest of the portfolio is
large and widening. Note that Cirrus' TTM margin (~49%) flatters the recent quarter (~44%) because
the trailing window still contains early, full-price months; and Solstice's TTM return rate (~12%)
is diluted by pre-defect months — the latest six months run at ~18%.

## 3. Solstice Ergonomic Chair — quality defect

- Returns: ~2–3% baseline → roughly 18% in the latest six-month seasonal window.
- Support tickets: `defect` and `mechanism_noise` up more than 10× (about 1 → 13+ per month),
  ~146 tickets/1,000 units on a trailing basis and >200 in the latest six months.
- Revenue: roughly 46% below the same six months of 2025 while list price ($399) and marketing
  spend are flat.
- Root cause: locking-pin alloy substitution by Nordic Spring Works (batch NS-2602) without change
  notification. Incoming inspection tested function, not fatigue life.
- Decision: containment + replacement program + supplier claim. Do **not** discount to clear inventory.
- Owner: VP Product / Head of Quality. Next checkpoint: supplier 8D due 2026-09-15.

## 4. Cirrus Air Purifier — discount dependency

- Discount rate: ~5% in mid-2025 → ~35% by mid-2026; gross margin ~62% → ~44%.
- Units are still up ~21% in the latest four-month window — but only because a promotion has been
  running continuously; list-price sell-through has collapsed.
- Marketing spend is the highest in the portfolio (~$17k/month) and is largely promotional support.
- Quality is normal (returns ~7%, tickets in the normal band), so this is a commercial problem.
- Decision: freeze deep overrides, 8-week list-price baseline, shift to filter-bundle value selling.
- Owner: VP Revenue / Finance. Success gate: margin ≥ 50% within two quarters without losing more
  than 15% of units.

## 5. Tide Portable Speaker — weak marketing

- Marketing spend ~$1.1k/month versus a $4k viability floor and $4.5k–12k for peer SKUs.
- Paid share of voice down ~35%, organic sessions down ~38% YoY, session conversion ~1.1% vs 2.4% benchmark.
- Unit sales are down ~12% year over year; average selling price is flat ($75.20 → $75.17) and the
  discount rate is stable at ~6%, with returns ~3% and tickets in the normal band. No quality or
  pricing issue.
- Decision: 90-day Tier B demand-generation test; if it fails, move to end-of-life.
- Owner: Head of Growth. Success gate: +25% sessions and conversion ≥ 1.8%.

## 6. What "underperforming" means for analysis

A SKU is underperforming when it is in the bottom three by trailing-12-month revenue **or** shows
negative year-over-year growth together with margin or return-rate deterioration. Root-cause analysis
must compare at least: price/discount trend, return rate, support-ticket rate and marketing spend.
Revenue alone is not a diagnosis.

## 7. Data caveats

- Revenue is gross of returns; returns are flagged on the original sale line and tracked separately.
- Marketing spend is recorded monthly per product and is not order-attributed.
- Ticket counts must be normalized per 1,000 units sold when volumes are changing.
- Reporting window: trailing 12 months of `sale_date`, not fiscal quarters.
