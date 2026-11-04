# Quality Escalation & Defect Management

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
