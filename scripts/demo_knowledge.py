"""Deterministic internal knowledge corpus for the AgentForge demo.

`scripts/generate_demo_data.py` writes these documents verbatim into
``data/knowledge/``. They are fictional but realistic internal memos for a
mid-market consumer-hardware company, and they carry the evidence the agent
workflow needs to explain the three deliberately underperforming products:

* Solstice Ergonomic Chair  -> supplier quality defect (returns + tickets + revenue decline)
* Cirrus Air Purifier       -> price/discount dependency (margin collapse)
* Tide Portable Speaker     -> weak marketing (low traffic/conversion, stable pricing)

Every document is planning to be ingested into the RAG store, so headings are
kept meaningful (the markdown chunker uses them as chunk context). Keep edits
markdown-safe; all documents are plain UTF-8.
"""

from __future__ import annotations

# Each entry is (path relative to data/knowledge, full markdown text).
# Paths use forward slashes so they are stable on Windows and POSIX.
KNOWLEDGE_DOCS: list[tuple[str, str]] = []

MARKET_DOCS: list[tuple[str, str]] = []


def _add(path: str, text: str) -> None:
    KNOWLEDGE_DOCS.append((path, text))


# ---------------------------------------------------------------------------
# 1. Pricing & discount policy
# ---------------------------------------------------------------------------
_add(
    "pricing_policy.md",
    """# Pricing & Discount Policy (FY26)

**Owner:** Revenue Operations · **Policy ID:** REV-PR-07 · **Effective:** 2025-02-01 · **Last reviewed:** 2026-04-02

## 1. Scope

This policy applies to every hardware SKU sold through direct e-commerce, retail
partners and marketplace channels in North America. It governs list-price changes,
promotional discounts, channel price parity and gross-margin exceptions. It does not
cover freight credits, which are handled by the logistics policy.

## 2. List price governance

- List prices are owned by Revenue Operations with input from Product Marketing and Finance.
- A list-price change requires 30 days' notice to retail partners and a re-priced catalog feed.
- Channel price parity must be within 2%; marketplace coupons and automatic cart discounts
  count toward the effective discount even when they are funded by the retailer.
- Emergency price moves during a competitive event require written CFO approval before publication.

## 3. Discount authority matrix

| Effective discount | Approver | Required evidence |
|---|---|---|
| 0–5% | Account Executive | none (standard volume discount) |
| 5–15% | Sales Manager | competitive quote or volume commitment |
| 15–25% | RevOps Director | unit-economics analysis and a stated end date |
| 25%+ | CFO | strategic justification, 90-day cap, margin recovery plan |

The effective discount is measured against list price after all coupons and rebates.
Overrides are logged in the pricing tracker; the default state of an override is "expires",
never "renews".

## 4. Gross margin floor

- The blended gross-margin floor is **50%** for every SKU, measured after discount and
  before marketing spend.
- Any promotion that would take a SKU below the floor must be filed as a margin exception
  with a recovery plan; volume alone is not a justification.
- Gross margin is defined as (revenue − cost) / revenue. A SKU whose margin is below the
  floor for two consecutive quarters is reviewed by the pricing council.

## 5. Promotional cadence

- At most two price promotions per SKU per quarter; each is capped at 21 days.
- "Rolling competitive match" discounts are limited to one quarter and must carry an explicit
  end date. Permanent promotions are prohibited.
- Promotional depth should not exceed 15% unless a CFO exception is active for that SKU.
- Every promotion must record a hypothesis (volume, acquisition, clearance) and a readout.

## 6. Watchlist — discount dependency

**Cirrus Air Purifier (SKU AF-1007).** The discount rate has climbed from roughly 5% in
mid-2025 to roughly 35% by mid-2026 under a chain of competitive-match overrides. Blended
gross margin has compressed from about 62% to about 44%, well below the 50% floor. Unit
volume only holds while a promotion is live: every pause has produced a double-digit
demand drop within two weeks. This is the textbook definition of discount dependency —
growth is rented, not earned, and the SKU is now trading margin for flat revenue.

Pricing council actions for AF-1007 (agreed 2026-08-12):

1. Freeze all new 25%+ overrides; only the CFO exception on file may be honoured.
2. Return to list price for an 8-week baseline window and measure true organic demand.
3. Shift spend from discount to value merchandising (filter subscription bundle, air-quality content).
4. Review filter attach rate, where gross margin is above 60%.

**Tide Portable Speaker (SKU AU-3019).** Pricing is stable at $79.99 with discounts around
6%; there is no discount dependency here. Its problem is demand generation and it should
not be fixed with price cuts.

**Solstice Ergonomic Chair (SKU FN-2042).** Price has been unchanged at $399 and discounts
have stayed in the 4–10% band. The chair's problem is product quality, not pricing.

## 7. Measuring discount impact

Discount rate = 1 − revenue / (quantity × unit_price). Group by SKU and month to see
whether revenue growth is coming from units or from deeper discounts. A SKU whose discount
rate rises while units are flat is buying revenue it cannot keep.

## 8. Reporting cadence

- Weekly: promotional calendar and override log.
- Monthly: margin-by-SKU review; any SKU below floor is escalated to the pricing council.
- Quarterly: price-pack architecture review with Product Marketing.
""",
)


# ---------------------------------------------------------------------------
# 2. Quality escalation
# ---------------------------------------------------------------------------
_add(
    "quality_escalation.md",
    """# Quality Escalation & Defect Management

**Owner:** Quality & Customer Operations · **Doc ID:** QMS-14 · **Version:** 3.2 · **Updated:** 2026-08-03

## 1. Purpose

This document defines how a product defect becomes an escalation, what evidence is
required, and how returns, support tickets and revenue trends are used to size the
problem. It is the authoritative process for quality holds and supplier claims.

## 2. Severity levels

| Severity | Definition | Initial response |
|---|---|---|
| S1 — Critical | Safety risk: electrical, fire, injury, or unexpected release under load | Stop-ship within 4 hours; legal and CEO notified |
| S2 — High | Product unusable or fails repeatedly; no safety risk | Stop-ship review within 2 business days |
| S3 — Medium | Significant annoyance or cosmetic failure; workaround exists | Investigation within 5 business days |
| S4 — Low | Minor issue, isolated reports | Tracked in backlog |

## 3. Escalation triggers

- A defect theme exceeding **3% of units shipped** in a rolling 30-day window opens a
  formal quality investigation.
- Exceeding **8% of units shipped** triggers a stop-ship review and a supplier 8D request.
- Support-ticket volume running at **2× the trailing 8-week baseline** for two consecutive
  weeks indicates a systemic issue even when no single failure code dominates.
- A SKU return rate above **10%** is automatically a quality escalation, regardless of the
  reason mix.

## 4. Active escalation — Solstice Ergonomic Chair (SKU FN-2042)

- **Supplier:** Nordic Spring Works (contract NSW-2024-06); affected production batch **NS-2602**
  (built 2025-09 to 2025-11).
- **Symptom:** the recliner tilt mechanism fails to lock in the upright position, creaks loudly
  under load, and in a small number of cases releases unexpectedly. No injuries had been
  reported as of 2026-08-01.
- **Evidence:** unit returns moved from a **~2–3% baseline to roughly 18%** in the most recent
  six-month window; support tickets tagged `defect` and `mechanism_noise` rose from about 1 per
  month to more than 13 per month in the same seasonal window (a **>10× increase**); and
  **revenue is roughly 46% below the same six months of 2025** even though list price and marketing
  spend are unchanged. The pattern is a quality event, not a pricing or demand event.
- **Containment:** stop-ship on remaining NS-2602 inventory; free replacement tilt mechanism for
  affected serial ranges; 36-month mechanism warranty; proactive outreach to purchasers.
- **Root cause (preliminary):** the supplier substituted a lower-grade alloy for the locking pin
  without the change notification required by clause 4.1 of the supply agreement. Incoming
  inspection tested function on arrival, not fatigue life after 6–10 weeks of use.
- **Supplier action:** 8D report due 2026-09-15; cost-recovery claim filed under clause 4.2.

## 5. Return and RMA handling

- Defect returns are accepted outside the standard 30-day window and are never restocked.
- Every defect return must be tagged with a failure code and routed to failure analysis.
- Return rates should be computed at the unit level, not the order level: a single order can
  contain several units and a partial return must not look like a full order return.
- Returns for preference reasons (no longer needed, changed mind) are not quality signals.

## 6. Ticket-to-defect linkage

Support tickets carry an `issue_type` and a `severity`. For SKU quality reviews, join
`support_tickets` to `products` and compare monthly ticket counts **normalized per 1,000 units
sold**. Absolute ticket counts are misleading when volume is falling. A SKU with rising tickets
and falling units is deteriorating on both axes.

## 7. Review cadence

Weekly quality stand-up (open escalations), monthly supplier scorecard, quarterly quality review
with Product and Finance. Every S1/S2 escalation produces a post-incident write-up within 30 days.
""",
)


# ---------------------------------------------------------------------------
# 3. Marketing playbook
# ---------------------------------------------------------------------------
_add(
    "marketing_playbook.md",
    """# Marketing Playbook — Hardware Portfolio (FY26)

**Owner:** Growth Marketing · **Doc ID:** MKT-03 · **Updated:** 2026-07-20

## 1. Budget tiers

| Tier | Portfolio revenue | Target monthly paid spend | Primary channels |
|---|---|---|---|
| A — Flagship | > $1.5M TTM | $10k–15k | brand search, retail co-op, paid social |
| B — Growth | $400k–1.5M TTM | $4.5k–9k | paid search, retargeting, lifecycle email |
| C — Maintenance | < $400k TTM | $1.2k–3k | brand defense only |

**Viability floor:** roughly **$4,000/month** of combined paid and lifecycle spend is the
minimum to defend category share for a consumer-hardware SKU. Below the floor, organic reach
decays, competitors buy our branded terms, and the SKU enters a demand spiral that looks like a
product problem but is not.

## 2. Funnel benchmarks

| Stage | Healthy | Warning | First action |
|---|---|---|---|
| PDP sessions → add to cart | 6–9% | 3–5% | PDP content and review review |
| Add to cart → purchase | 35–45% | < 25% | checkout friction, offer fit |
| Session conversion (all traffic) | 2.0–2.6% | < 1.5% | traffic quality and offer fit |
| Paid CAC payback | ≤ 90 days | > 150 days | channel-mix review |

Conversion should be compared within category. Audio converts around 2.4%, lighting around
2.1%, and furniture around 1.6% because of higher consideration time.

## 3. Channel mix

Paid search 40%, paid social 25%, retail co-op 20%, email/lifecycle 15%. Shift budget toward
retail co-op when paid social CAC rises more than 20% quarter over quarter. Branded search is
defensive: cutting it is the fastest way to lose share of voice and let competitors bid on our name.

## 4. Campaign cadence

Always-on brand defense plus two seasonal pushes (spring and Q4). New SKU launches receive a
90-day learning budget of about $6k/month. Every campaign gets a written hypothesis and a readout;
budgets that cannot name their hypothesis are cut at the monthly review.

## 5. Watchlist — demand-generation gap

**Tide Portable Speaker (SKU AU-3019).** Paid spend has been capped at roughly **$1.1k/month**
since early FY26 — well below the $4k viability floor and far below the $4.5k–12k that peer
SKUs receive. Paid-search share of voice is down about **35%**, organic sessions are down about
**38% year over year**, and session conversion sits near **1.1%** versus a **2.4%** category
benchmark. Pricing ($79.99) and discounting (~6%) are stable, and quality metrics are normal
(returns ~3%, tickets low). The loss is a traffic/conversion problem, not price and not product
quality — price cuts would burn margin without creating demand.

Recommended test: a 90-day Tier B demand-generation sprint (paid search + creator content +
retail co-op) with a success gate of +25% sessions and conversion ≥ 1.8%. If the test fails,
move the SKU to end-of-life planning rather than keep funding a sub-scale campaign.

## 6. Measurement definitions

- **Traffic** — sessions across PDP, category and landing pages (analytics `sessions`).
- **Conversion** — orders / sessions.
- **Marketing efficiency** — incremental revenue / spend, read on a 30-day lag.
- **Share of voice** — paid impressions / category paid impressions for the SKU's keyword set.
- Spend is recorded per product-month in `marketing_spend`; it is not attributed at the order level.
""",
)


# ---------------------------------------------------------------------------
# 4. Product notes
# ---------------------------------------------------------------------------
_add(
    "product_notes.md",
    """# Product Notes — Portfolio Briefs

**Owner:** Product Management · **Updated:** 2026-09-10

Eight active SKUs across Lighting, Audio, Furniture and Kitchen. Each brief lists
positioning, current commercial status and known issues. Status values: healthy, watchlist,
end-of-life candidate.

## Aurora Arc Desk Lamp (LT-1001)

- Category: Lighting · List price: $89.00 · Launched: 2023-03-15 · **Status: healthy**
- Adjustable warm/cool task lamp with USB-C passthrough; strongest attach rate with the Terra desk.
- Units and revenue are growing steadily; returns around 3%; ticket volume normal; discounting low.
- Risk: a competitor launched a similar lamp at $69; watch the price gap but do not match it.

## Nimbus Wireless Headphones (AU-2001)

- Category: Audio · List price: $149.00 · Launched: 2023-09-05 · **Status: healthy**
- Flagship audio SKU and the portfolio's largest revenue contributor.
- Strong repeat purchase and low return rate (~3%); occasional firmware tickets handled by OTA updates.
- Marketing receives a Tier A budget; maintain share of voice during Q4.

## Terra Bamboo Standing Desk (FN-2030)

- Category: Furniture · List price: $449.00 · Launched: 2024-01-22 · **Status: healthy**
- B2B and home-office demand; high average order value. Assembly-support tickets are expected and
  do not indicate a defect.
- Supplier Greenline has a clean scorecard; no active quality holds.

## Vela Espresso Machine (KT-4001)

- Category: Kitchen · List price: $329.00 · Launched: 2024-04-10 · **Status: healthy**
- Steady growth; descaling and portafilter questions dominate support. Consumables attach is strong.

## Lumen Smart Bulb 4-Pack (LT-1002)

- Category: Lighting · List price: $49.99 · Launched: 2022-11-01 · **Status: healthy**
- Volume leader by units; low price point, high replacement cadence. Setup and connectivity tickets
  are normal for the category. Bundle with Aurora for basket growth.

## Solstice Ergonomic Chair (FN-2042)

- Category: Furniture · List price: $399.00 · Launched: 2024-08-19 · **Status: watchlist — quality escalation**
- Supplier Nordic Spring Works, affected batch NS-2602. Tilt-mechanism locking defect appears after
  6–10 weeks of use; returns have risen from ~2–3% to roughly 18% and support tickets tagged
  `defect`/`mechanism_noise` are up more than tenfold (about 1 → 13+ per month in the same seasonal
  window).
- Price and marketing spend are unchanged; the problem is quality, not demand. Revenue is down
  roughly 46% year over year on a same-season basis as the category rating fell from 4.6 to 3.2.
- Risk: replacement-program cost, marketplace rating damage and possible returns of replacement units.
  Do not discount to move defective inventory — it converts a quality problem into a trust problem.

## Cirrus Air Purifier (AF-1007)

- Category: Kitchen/Home · List price: $249.00 · Launched: 2024-02-14 · **Status: watchlist — margin/discount dependency**
- Product quality is fine: returns around 7% (category-normal) and tickets normal. The commercial issue
  is promotional reliance.
- Discount depth has risen to roughly 35% and gross margin has fallen from about 62% to about 44%.
  Units only grow while promotions are live; list-price sell-through has collapsed.
- Main profitability lever is the filter subscription (attach above 40%, margin above 60%). Any pricing
  reset must protect the subscription funnel.
- Action: freeze deep overrides, run an 8-week list-price baseline, bundle filters instead of cutting price.

## Tide Portable Speaker (AU-3019)

- Category: Audio · List price: $79.99 · Launched: 2022-06-30 · **Status: watchlist — stagnant demand**
- Aging SKU (launched 2022) with stable pricing (~6% discount), normal quality (returns ~3%, tickets low)
  and the lowest revenue in the portfolio.
- Marketing spend is about $1.1k/month, below the $4k viability floor; paid share of voice and organic
  sessions are both down sharply, unit sales are down roughly 12% year over year, and conversion is
  well below category benchmark.
- Options: fund a 90-day demand-generation test, bundle with Nimbus, or plan end-of-life. Discontinuation
  is a legitimate outcome if demand cannot be re-established at acceptable CAC.
""",
)


# ---------------------------------------------------------------------------
# 5. Support taxonomy
# ---------------------------------------------------------------------------
_add(
    "support_taxonomy.md",
    """# Support Ticket Taxonomy & Service Levels

**Owner:** Customer Operations · **Doc ID:** CS-07 · **Updated:** 2026-06-15

## 1. Issue types

| Code | Meaning | Typical cause |
|---|---|---|
| `defect` | Product does not function as designed | Manufacturing or component failure |
| `battery` | Charging or runtime problems | Cell age, firmware, charger compatibility |
| `connectivity` | Bluetooth/Wi-Fi pairing failures | Firmware, interference, user error |
| `setup` | Cannot complete first-time setup | Instructions, app flow, missing parts |
| `noise` | Unexpected acoustic noise | Fan, motor, electrical hum |
| `usability` | Works but is hard to use | Design, documentation, accessibility |
| `firmware` | Update failures or regressions | Release quality, update server |
| `shipping_damage` | Damaged in transit | Packaging, carrier handling |
| `missing_parts` | Components missing from the box | Pack-out error, supplier shortage |
| `electrical` | Electrical fault, power issues | Component failure, safety relevant |
| `mechanism_noise` | Mechanical creak, rattle or binding | Structural/mechanical defect |

## 2. Severity

| Severity | Definition | SLA first response | Resolution target |
|---|---|---|---|
| `critical` | Safety risk or total loss of function | 2 hours | 48 hours |
| `high` | Major function broken, no workaround | 8 business hours | 5 business days |
| `medium` | Degraded experience, workaround exists | 1 business day | 10 business days |
| `low` | Question, cosmetic, feature request | 3 business days | best effort |

## 3. Expected volumes

For consumer hardware, steady-state ticket volume is roughly **6–10 tickets per 1,000 units**
sold, with `setup` and `usability` dominating. A SKU sustaining more than **50 tickets per
1,000 units** is a quality event; more than **120 per 1,000 units** is a stop-ship candidate.

The Solstice Ergonomic Chair (FN-2042) is currently running at roughly **150 tickets per 1,000
units** on a trailing basis and above **200 per 1,000** in the latest six months, with `defect` and
`mechanism_noise` dominating. Every other SKU sits between **7 and 15 per 1,000 units**, inside the
normal band. Ticket counts must be normalized per 1,000 units because a shrinking SKU can show
falling absolute tickets while deteriorating on a rate basis.

## 4. Triage rules

- `critical` and safety-adjacent `electrical` tickets page the on-call quality engineer immediately.
- Three tickets for the same issue type on the same SKU within 24 hours create a linked problem record.
- Ticket text is never used as the only evidence for a quality decision; it must be paired with
  return-rate or failure-analysis data.
- Quality escalations consume a monthly summary from this taxonomy, not raw ticket exports.

## 5. Tagging quality bar

A ticket is considered tagged correctly when issue type and severity are both set and the SKU is
identified. Untagged tickets are excluded from rate calculations so they do not dilute a signal.
""",
)


# ---------------------------------------------------------------------------
# 6. Quarterly business review (strongest evidence document)
# ---------------------------------------------------------------------------
_add(
    "quarterly_business_review.md",
    """# Q3 FY26 Quarterly Business Review — Hardware Portfolio

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
""",
)


# ---------------------------------------------------------------------------
# 7. Supplier agreements
# ---------------------------------------------------------------------------
_add(
    "supplier_agreements.md",
    """# Supplier Quality Agreements — Summary

**Owner:** Procurement · **Updated:** 2026-08-20 · **Review cycle:** annual

## 1. Standard quality clauses

- **4.1 No unannounced changes.** The supplier must notify in writing before any change to
  bill of materials, material grade, production process or sub-supplier. Unannounced changes are a
  material breach.
- **4.2 Quality holdback and recovery.** A 3% quality holdback is withheld from invoice value.
  Verified defect costs — replacement parts, freight, refurbishment, customer credits — are recoverable
  against the holdback and, beyond it, against future invoices.
- **4.3 Stop-ship and replacement.** The supplier must support stop-ship, supply replacement components
  at no charge for affected batches, and provide a correct 8D report within 30 days of notification.
- **4.4 Batch traceability.** Every unit must be traceable to a production batch and date code.

## 2. Nordic Spring Works — chairs (FN-2042)

- Contract NSW-2024-06, signed 2024-04-02.
- Batch **NS-2602** produced 2025-09-10 to 2025-11-28; approximately 4,800 units.
- Preliminary finding: the locking-pin alloy was substituted without a change notification, a breach
  of clause 4.1. Failure mode is fatigue-related and appears after 6–10 weeks of use.
- Cost recovery: replacement mechanism kits (~$18/unit at supplier cost), inbound/outbound freight,
  extended-warranty reserve and customer credits are being claimed under clause 4.2. The claim is
  being quantified by Finance; the quality holdback covers only part of the exposure.
- Status: supplier 8D due 2026-09-15; sourcing dual-sourced for the next production run.

## 3. Other suppliers

| Supplier | SKUs | Quality status | Notes |
|---|---|---|---|
| Brightpath | Aurora (LT-1001), Lumen (LT-1002) | green | clean scorecard, no holds |
| Kestrel Acoustics | Nimbus (AU-2001), Tide (AU-3019) | green | normal battery/connectivity ticket profile |
| Greenline | Terra (FN-2030) | green | assembly support tickets are expected |
| Brewline | Vela (KT-4001) | green | descaling questions are user education |
| Pureflow | Cirrus (AF-1007) | green | no quality issue; commercial/pricing issue only |

## 4. Audit calendar

Annual on-site audit for every Tier 1 supplier; semi-annual for suppliers with an open escalation.
Nordic Spring Works receives an unscheduled audit after the 8D is accepted.

## 5. Escalation contacts

Procurement owns the commercial claim; Quality owns the technical investigation; Finance signs off
recovery amounts. No supplier communication about root cause is sent without Quality sign-off.
""",
)


# ---------------------------------------------------------------------------
# 8. Returns & warranty policy
# ---------------------------------------------------------------------------
_add(
    "returns_and_warranty_policy.md",
    """# Returns & Warranty Policy

**Owner:** Customer Operations · **Doc ID:** CS-02 · **Updated:** 2026-05-30

## 1. Standard return window

Customers may return unused or lightly used products within **30 days** of delivery. Refunds are
issued to the original payment method after inspection. Opened products in resalable condition are
subject to a 15% restocking fee.

## 2. Return reason taxonomy

| Reason | Code | Restock? | Notes |
|---|---|---|---|
| No longer needed / changed mind | `preference` | yes | 15% restocking fee |
| Defective or dead on arrival | `defect` | no | full refund; routed to failure analysis |
| Damaged in transit | `shipping_damage` | no | carrier claim opened |
| Wrong item shipped | `fulfillment` | yes | expedited replacement |
| Missing parts | `missing_parts` | yes if part available | part shipped first when possible |

Only `defect`, `shipping_damage` and repeated `missing_parts` are quality signals. Preference returns
are a merchandising/demand signal and must not be mixed into defect analysis.

## 3. Defect returns outside the window

Defect returns are accepted beyond the 30-day window when the fault is covered by warranty — up to
36 months for the Solstice chair mechanism under the extended-warranty decision of 2026-08-14, and
12 months for other SKUs. Defective units are never restocked; they are scrapped or returned to the
supplier for failure analysis.

## 4. Recording returns in the data warehouse

- `sales.returned = 1` marks the units on that sale line as returned. The flag is recorded on the
  original sale date, not the date the return was processed, so monthly return rates lag reality
  slightly.
- Revenue on the sale line is gross (after discount, before return credit); finance books the credit
  separately.
- **Return rate** = returned units / units sold. The portfolio baseline is **2–4%**. A SKU above
  **10%** is a quality escalation; above **15%** is a stop-ship review.

## 5. Warranty and replacement programs

- Standard warranty: 12 months against manufacturing defects.
- Solstice chair: free replacement tilt mechanism plus 36-month mechanism warranty (announced 2026-08).
- Extended-warranty reserve is accrued per SKU and reviewed quarterly with Finance.

## 6. Fraud and abuse controls

Repeated returns of the same SKU without a defect code are reviewed by Customer Operations. Abuse
controls never block a safety-related replacement.
""",
)


# ---------------------------------------------------------------------------
# 9. Metric definitions & canonical queries
# ---------------------------------------------------------------------------
_add(
    "metrics_definitions.md",
    """# Metric Definitions & Canonical Queries

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
""",
)


# ---------------------------------------------------------------------------
# Market notes (offline corpus surfaced by the web_search tool, marked simulated)
# ---------------------------------------------------------------------------
MARKET_DOCS.append(
    (
        "market/ergonomic_chairs.md",
        """# External Market Note — Ergonomic Seating (2025–2026)

**Coverage period:** 2025–2026 · **Source:** AgentForge curated market corpus (offline, simulated) · **Not a live web result**

## Category signal (2025–2026)

- Mid-market ergonomic seating remains a considered purchase with a 12–18 month replacement cycle.
- Quality-related returns above 10% of units typically cost 2–3× unit gross margin once inbound freight,
  refurbishment, replacement and customer credits are counted.
- Review ratings below 4.0 correlate with two to three times the category return rate in the same period.
- Buyers increasingly filter by mechanism warranty; a 36-month mechanism warranty is a credible trust
  signal after a defect event.
- Category return-rate benchmark for 2025–2026: 4–6% of units for seating priced $250–$600.

## Takeaway for 2026 planning

- A quality event should be addressed with containment, replacement and supplier recovery. Discounting a
  product with a mechanical defect does not restore demand and risks converting a quality problem into a
  trust and safety narrative.
- Demand softness combined with stable pricing and stable marketing points to product quality or demand
  generation, not to price.
- External 2025–2026 category benchmarks only: do not attribute these figures to a specific internal SKU.
""",
    )
)

MARKET_DOCS.append(
    (
        "market/air_purifiers.md",
        """# External Market Note — Air-Treatment Devices (2025–2026)

**Coverage period:** 2025–2026 · **Source:** AgentForge curated market corpus (offline, simulated) · **Not a live web result**

## Category signal (2025–2026)

- Air-treatment devices are a promotion-heavy category. Average promotional depth across 2025–2026 is
  12–18%; category leaders use seasonal events rather than standing discounts.
- Standing discounts above 30% are associated with margin compression and price anchoring — buyers
  learn to wait for the next promotion.
- Filter/subscription attach is the profit engine: 35–45% attach with 60%+ margin on consumables during
  2025–2026.
- Quality is broadly stable in the category; complaints are usually noise-level or consumable-life
  issues rather than defects.

## Takeaway for 2026 planning

- If unit volume only holds while a promotion is live, treat it as discount dependency: reset to list
  price for a measured baseline, protect the consumables funnel, and use value bundles rather than
  deeper price cuts.
- Benchmark promotion depth for the category: 15–20% with explicit end dates.
- External 2025–2026 category benchmarks only: do not attribute these figures to a specific internal SKU.
""",
    )
)

MARKET_DOCS.append(
    (
        "market/portable_audio.md",
        """# External Market Note — Portable Audio (2025–2026)

**Coverage period:** 2025–2026 · **Source:** AgentForge curated market corpus (offline, simulated) · **Not a live web result**

## Category signal (2025–2026)

- Paid social CAC for portable audio rose roughly 25% year over year through 2025–2026; paid search
  remains the most efficient acquisition channel for branded and category terms.
- Paid share of voice correlates with sessions: brands that cut brand-defense spend lost ~30–40% of
  organic sessions within two quarters during this period.
- Session conversion benchmark for portable audio in 2025–2026: 2.0–2.6%; warning band below 1.5%.
- The $49–$99 band remains the highest-volume segment; discounts above 12% rarely pay back CAC.

## Takeaway for 2026 planning

- A product with stable price, normal quality and conversion below benchmark is a demand-generation
  problem, not a pricing problem: price cuts cannot buy back paid reach.
- External 2025–2026 category benchmarks only: do not attribute these figures to a specific internal SKU.
""",
    )
)

MARKET_DOCS.append(
    (
        "market/competitor_watch.md",
        """# External Market Note — Competitor Watch, 2025–2026 (simulated digest)

**Coverage period:** 2025–2026 · **Source:** AgentForge curated market corpus (offline, simulated) · **Not a live web result**

## Pricing moves (2025–2026)

- Two mid-market seating brands cut entry prices 8–12% during H1 2026.
- Air-treatment competitors ran rotating 20% promotions every 6–8 weeks through 2025–2026 rather than
  standing discounts; the category is normalizing after the 2025 promotion cycle.
- Portable-audio competitors increased paid search bids on category terms through 2026, raising CPC
  roughly 18% year over year.

## Quality recalls (2025–2026)

- A seating competitor recalled one production batch of tilt mechanisms in early 2026 for a locking-pin
  issue; the recall was contained with a replacement program and the brand's review rating recovered
  within two quarters after a transparent response.

## Takeaway for 2026 planning

- External benchmarks from 2025–2026 support separating quality, pricing and demand-generation
  diagnoses: the same commercial lever should not be applied to every category.
- This is a simulated digest; do not attribute it to a specific internal SKU and do not cite dates
  outside the 2025–2026 period.
""",
    )
)


def all_docs() -> list[tuple[str, str]]:
    """All generated knowledge files as (relative path, markdown text), sorted."""
    return sorted(KNOWLEDGE_DOCS + MARKET_DOCS, key=lambda item: item[0])


def titles() -> dict[str, str]:
    """Map relative path -> first markdown H1 (or a humanized filename fallback)."""
    out: dict[str, str] = {}
    for rel_path, text in all_docs():
        title = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
        if not title:
            stem = rel_path.rsplit("/", 1)[-1].removesuffix(".md")
            title = stem.replace("_", " ").title()
        out[rel_path] = title
    return out


if __name__ == "__main__":  # pragma: no cover - tiny convenience for inspection
    docs = all_docs()
    print(f"{len(docs)} knowledge files")
    for rel_path, text in docs:
        print(f"  {rel_path:45s} {len(text):6d} chars  title={titles()[rel_path]!r}")
