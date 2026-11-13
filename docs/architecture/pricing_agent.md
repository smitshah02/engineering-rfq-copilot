# Pricing Agent (A4)

`rfq_copilot.pricing_agent` (Day 40). It computes the evidence-backed manufacturing cost and
the deterministic, non-binding selling price for parts that A2 found manufacturable and A3
scheduled. It does not quote, approve, release, reserve or write anything.

```
ManufacturingAssessment (A2) + SupplyAssessment (A3: available | constrained)
  + trusted price-v1.0 RuleVersionDetails + trusted ToolExecutor
  -> evaluate_pricing(a2, a3, execute, *, pricing_rules, timeout_seconds) -> PricingAssessment
```

It is a plain async function, like A2 and A3. It uses no LangGraph node and no model, and it
does not import `rfq_copilot.graph`.

## Authority

| Tool | Server | Permission | Service |
|---|---|---|---|
| `estimate_costs` | pricing | `view_quote` | `services.costing.estimate_costs` |
| `calculate_price` | pricing | `view_quote` | `services.pricing.calculate_quote` |

- A4 calls each tool once per RFQ, in this order, with no retries and inside one timeout.
- The argument is `PlanningInput(parts=<A2's specifications>, horizon_start=<A3's trusted
  horizon>)`. It never carries an `rfq_id`, identity, rule version or upstream result.
- The executor is `graph.mcp_executor` (the delegated credential bound to a verified
  principal). The server re-checks RBAC v2. A principal without `view_quote` (for example
  `manufacturing_engineer`) gets `forbidden` and sees no cost and no price.
- `evaluate_quotation` is not used: it duplicates both tools and adds scheduling that A3
  already supplies.
- Every amount comes from these services. A4 only checks the amounts and never derives one.
  It has no discounts, taxes, freight or shipping to apply, because the pricing rule defines
  none, and the explanation says so.

## Trusted pricing context

The caller passes the `price-v1.0` `RuleVersionDetails` that `mcp.runtime.pinned_rules`
loaded. A4 accepts it only if all of these hold:

- version, kind and source sha256 equal `repositories.rule_manifest.TRUSTED_RULES`
- the stored fingerprint equals the manifest's
- `canonical.fingerprint(document)` re-derives that fingerprint

Otherwise the result is `not_evaluated` / `pricing_rules_untrusted` and no tool is called.
Margin, currency, scales, rounding mode and inclusion flags are read from this rule with the
services' helpers (`gross_margin`, `rounding`, `_rate`).

## Input gate (`not_evaluated`, no tool called)

1. A2 passes A3's own gate (`supply_agent._gate`): feasible, evidence bound to unchanged
   specifications, rule pin, no duplicate parts.
2. A3 has the same `source_id` and `rfq_id` (`supply_assessment_mismatch`).
3. A3's status is `available` or `constrained` (otherwise `supply_<status>`).
4. A3 has evidence and a horizon (`supply_evidence_missing`).
5. A3's parts are A2's parts, same indexes and order (`supply_parts_mismatch`).
6. A3's evidence still holds (`supply_evidence_invalid`):
   - its arguments digest is the digest of A2's specifications and A3's dates
   - `supply_agent._inconsistent` accepts the three results
   - `_status` and `_explain` reproduce A3's status and explanation
   - each part's stock line is the one that covers it
7. A3 ran under the trusted pricing rule (`pricing_rule_mismatch`).

`constrained` supply is priced, because the costing service prices short stock. It is never
hidden: the result has `supply_status`, a `supply_constrained` review reason, a per-line
`stock_status` and a sentence in the explanation.

## Evidence checks

Both tool outputs are re-validated against their frozen contracts (`extra="forbid"`; an
off-contract output is `invalid_output`). Then:

| Check | Outcome |
|---|---|
| the two results under different rules | `error` / `invalid_evidence` |
| both under a rule other than the trusted one | `requires_review` / `pricing_rule_drift`: no top-level lines or totals; `evidence` keeps the unverified tool outputs, amounts included, for the reviewer |
| a consistent `needs_clarification` / `not_manufacturable` verdict | `requires_review` / `pricing_contradicts_upstream` |
| any other non-priced or partial result | `invalid_evidence` |
| cost lines are not exactly A2's parts in order (missing, duplicated, extra, swapped) | `invalid_evidence` |
| a line's quantity is not the specification's, or its route is not A2's verified route | `invalid_evidence` |
| packaging is not the rule's rate, or there is a finishing cost without a `finish` operation | `invalid_evidence` |
| material + labour + finish + packaging ≠ variable; quantity × variable + setup ≠ line cost; Σ lines ≠ RFQ cost (exact, in the service's `PLANNING` context) | `invalid_evidence` |
| currency, gross margin or tax/freight/shipping flags differ from the rule | `invalid_evidence` |
| `calculation_id` is not `services.pricing.calculation_id(A2's parts, version)` | `invalid_evidence` |
| price lines are not the cost lines, or a line total / unit price is not `services.pricing.price_line(line cost, quantity, margin, scales, mode)` | `invalid_evidence` |
| quote total ≠ Σ line totals | `invalid_evidence` |

To make reuse possible, `price_line` was taken out of `evaluate_pricing` unchanged. It is the
only implementation of the line formula, and both the service and the A4 check call it.

Some per-unit rates (setup minutes, hourly rates, finishing per m²) are not re-derived; that
would mean reimplementing the costing model. They remain the tool's authority, and A4 checks
only that the tool's numbers are consistent with one another.

## Output (`PricingAssessment`, frozen, serializable)

- `source_id`, `rfq_id`, `status` (`priced` | `requires_review` | `not_evaluated` | `error`),
  `supply_status`
- rule version and fingerprint, `currency`, `unit_scale`, `line_scale`, `rounding`,
  `gross_margin`, the inclusion flags and `calculation_id`
- `lines`, one per part with its A2 index and `part_id`:
  - quantity and operations
  - cost components, display-rounded by `services.costing.display_costs` (internal cost)
  - `quoted_unit_price_usd` (advisory) and `line_total_usd` (the selling total)
  - `stock_status` from A3
- `total_manufacturing_cost_usd` (internal, unit scale) and `quote_total_usd` (customer-facing)
- `review_reasons`, `explanation`, `not_evaluated_reason`, `error`
- `evidence`: arguments digest, the A3 digest it is bound to, A3's inventory and capacity
  snapshot ids, and the exact `CostOutput` and `PriceOutput`

Explanations use fixed templates over tool values. Customer text, including `part_id`, never
appears in them.

## Limitations

- Rule drift: the `requires_review` result has no top-level amounts, but `evidence` keeps the
  raw `CostOutput` / `PriceOutput`, amounts included. Only a principal that passed the
  server's `view_quote` check receives them. The drift explanation's "no amount is
  reported" refers to the top-level fields only.

- The pricing tools do not report the snapshot they used. A4 links its results to A3's
  snapshots through the shared specifications and rule, not by snapshot id.
- RBAC v2 has one permission (`view_quote`) for both internal cost and selling price. A4
  cannot show the price while hiding the cost. Any narrower disclosure needs a new
  permission and contract.
- No discount, tax, freight, shipping or commercial approval exists in `price-v1.0`.
  Commercial policy (`approval-v1.2`) is out of scope for A4.
