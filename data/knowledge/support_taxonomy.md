# Support Ticket Taxonomy & Service Levels

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
