# Returns & Warranty Policy

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
