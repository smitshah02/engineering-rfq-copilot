# Quotation and Review Agent (A5)

`rfq_copilot.quotation_agent` (Day 41). A5 turns the verified results of A2 (manufacturing),
A3 (supply) and A4 (pricing) into a **non-persisted quotation draft**. It also states the
review that draft needs under the pinned approval policy.

A5 is not an approval, a release, a quote number or a database record. A human decides.

```text
ManufacturingAssessment (A2) + SupplyAssessment (A3) + PricingAssessment (A4)
    + trusted price-v1.0 and approval-v1.2 RuleVersionDetails + trusted ToolExecutor
    -> evaluate_quotation_review(a2, a3, a4, execute, *, pricing_rules, approval_rules,
                                 timeout_seconds=60) -> QuotationReviewAssessment
```

## 1. Scope

A5 does:

- Re-verify the upstream chain.
- Read the quotation service once.
- Assemble the draft lines and totals.
- Evaluate the approval-policy conditions it has trusted facts for.
- Name the required approver role and the outstanding release prerequisites.

A5 does not do any of the following. No authorized tool exists for them, and approval and
release are human-only:

- Create or version a draft in the database.
- Request approval.
- Track review outcomes.
- Approve, reject, release, sign, email or send anything.

## 2. Inputs

| Input | Source | Trust |
|---|---|---|
| `a2`, `a3`, `a4` | A2–A4 results of one source and RFQ | Re-verified (below); never trusted as well-formed objects |
| `pricing_rules` | `mcp.runtime.pinned_rules(session, "price-v1.0")` | Must match `TRUSTED_RULES` (kind, source sha256, re-derived fingerprint) |
| `approval_rules` | `pinned_rules(session, "approval-v1.2")` (`contracts.NEW_EVALUATION_APPROVAL_RULE_VERSION`) | Same manifest check, then `require_human_release` |
| `execute` | `graph.mcp_executor` (principal-bound, delegated credential) | The server enforces RBAC v2 `view_quote` |

There is no Intake (A1) input. A1's verified values reach A5 only through A2 (specifications)
and A3 (verified requested delivery date). A5 takes no free text, so customer statements
such as "approved", "urgent" or "special discount" cannot reach it as authority. A5 needs no
clock, because no rule defines a quote validity period (see Limitations).

## 3. Deterministic services and tools

- **A4 replay**: `pricing_agent.evaluate_pricing(a2, a3, replay, pricing_rules)`. The replay
  executor returns A4's own recorded tool outputs, with no network call. The result must
  equal `a4` exactly. This re-runs:
  - A3's gate: A2 evidence bound to its specifications by sha256 and A2's `_forged`.
  - A3's evidence checks: `_inconsistent`, `_status`, `_explain`, stock lines per part.
  - A4's checks: the trusted rule, A3 bound to A2, cost and price arithmetic against
    `price_line`, calculation id and flags.

  One implementation per rule: A5 rewrites none of them.
- **Quotation service**: the read-only MCP tool `evaluate_quotation` (pricing server,
  `services.quotation.quote_parts`, RBAC v2 `view_quote`, in `graph.READ_ONLY_TOOLS`).
  - Called once with A2's specifications, A3's horizon and A3's verified delivery date: the
    same `DeliveryPlanningInput` as A3, with the same digest.
  - No retries, one timeout. Allowlist: `ALLOWED_TOOLS = {"evaluate_quotation"}`.
- **Approval policy**: the pure `services.evaluation` helpers:
  - `require_human_release`
  - `review_threshold` (manager review at or above 7500.00 USD)
  - `borderline_ratio` and `borderline_bend_lines` (TD-004, ratio 1.25)
  - `engineering_conditions`

  `borderline_bend_lines` receives a minimal view with exactly the attributes it reads:
  line numbers, A2's verified minimum radii and A2's specifications.

`services.evaluation.evaluate_outcome` itself is not called. It needs the customer's ORM
row (account status, credit limit and exposure) and a service `Quotation`. No agent tool
exposes either; the commercial-policy MCP tool (`evaluate_stored_rfq`) is deferred
(`mcp/pricing.py`).

## 4. Quote assembly versus pricing

Every amount in the draft is A4's verified value: unit price, line total, quote total,
currency and calculation id. A5 computes no price, rounding, tax, freight, shipping,
discount, limit or validity.

The quotation service result is a cross-check from a **separate snapshot**. Its outcome
must agree with A2, A3 and A4, else `error` / `invalid_evidence`:

- line numbers 1..n
- part order
- `valid` manufacturing with A2's findings
- stock status
- line totals
- per-line procurement delay (D41-01, below)
- `blocking_lines == ()`
- calculation id, total and currency
- ready date, lead days and findings

**Per-line procurement (D41-01).** The service sets each line's `procurement_business_days`
in two steps:

1. `assemble_quotation` maps each part to its (material, thickness) stock line.
2. It takes that line's delay from `lead_time.plan_start`, which is
   `services.lead_time.procurement_business_days(line)`: 0 when the line is sufficient,
   otherwise the longest positive supplier lead, with the service default when none is
   positive.

Parts that share stock share one line, so they share its delay. The RFQ-level delay is the
maximum over all lines.

A5 applies that same service function to A3's per-part stock line, which the A4 replay
re-verifies. Every quoted line must carry exactly that value, and a missing value is
rejected. The maximum must also equal A3's lead-time `procurement_business_days`.

This keeps legitimate differences between lines. In a mixed RFQ, a covered line stays 0 and
a short line carries its own supplier delay. Neither is replaced by the RFQ maximum. A5 does
not reimplement the scheduling algorithm.

`calculation_id` is A4's deterministic id of the parts and pricing rule. It is **not** a quote
number, and no quote number is created.

Draft lines carry no internal cost: the result has no `cost` or `margin` field.
`QuotationReviewAssessment.customer_view()` gives a separate `CustomerQuotationDraft` that
contains only:

- source and RFQ id, currency
- part ids, quantities, unit prices and line totals, the total
- exclusions
- the estimated ready date, the requested date and whether it is feasible
- `conditional`
- a fixed notice ("not a quote, not approved, not released, not a commitment")

**`conditional` (D41-02).** `conditional` is `False` only when ordinary approval and release
by `sales_manager` is the sole remaining step. It is `True` when any of these is true:

- The status is not `draft_ready_for_review`.
- There is a review reason (supply constrained, findings, borderline bend).
- A mandatory manager review applies (`manager_review_required`).
- Any policy check is unevaluated (`unevaluated_review_conditions`).

Customer account status and credit limit can't be evaluated today, so **every current draft
is `conditional=True`**. `False` is reachable only once those checks have a trusted source.

Even `False` is never approval. Release prerequisites always remain, and the fixed notice
always says the draft is not approved and not released. The customer view lists no internal
review detail; the internal result keeps it.

## 5. Policy evaluation versus human approval

| Concept | Where |
|---|---|
| Manager review (total >= threshold) | `manager_review_required`, `manager_review_reasons`, `manager_review_threshold_usd` |
| Engineering/commercial review reasons | `review_reasons`: A4's (`supply_constrained`, `pricing_rule_drift`, ...), the quotation findings (`DELIVERY_DATE_INFEASIBLE`, `CAPACITY_OUT_OF_HORIZON`, ...), `BORDERLINE_BEND_RADIUS` |
| Conditions without trusted facts | `unevaluated_review_conditions = ("customer_account_status", "credit_limit")`: never reported as passed |
| Who must approve | `required_approver_role = "sales_manager"`, the only role an approval request may require (`models.approvals` rule_02, `queries.list_pending_approvals`) |
| Release prerequisites | `release_prerequisites` = the policy's `quote_release_requires`; A5 verifies none |
| Approval / release | `human_approval_required: Literal[True]`, `approved: Literal[False]`, `released: Literal[False]`: the schema cannot represent an agent approval |

A policy evaluation is not an approval. No approval record is read: no read-only tool
exposes `approval_requests`. So the fourth case, "a human decision already recorded", is
not supported.

## 6. Statuses

| Status | When | Draft? | Tool called? |
|---|---|---|---|
| `draft_ready_for_review` | Verified draft, no review reason (manager review may still apply) | yes | yes |
| `requires_review` | A2/A3/A4 `requires_review`, or a verified draft with review reasons (supply constrained, findings, borderline bend) | only for a verified A4 `priced` | only for a verified A4 `priced` |
| `clarification_required` | A2 `needs_clarification` (fields in `clarifications`) or A3 `needs_clarification` (e.g. `requested_delivery_date_unverified`) | no | no |
| `not_evaluated` | Untrusted rules, source/RFQ mismatch, A2 infeasible/error/not evaluated, A3/A4 unpriceable, A4 not reproducible (`pricing_assessment_invalid`) | no | no |
| `error` | No executor (`unauthenticated`), MCP failure code, `timeout`, `unavailable`, `invalid_output`, `invalid_evidence` | no | yes (except `unauthenticated`) |

Precedence:

1. Rule trust
2. Identity
3. Clarification
4. Upstream review
5. Upstream block
6. A4 replay
7. A4 `requires_review`
8. Executor
9. Tool
10. Consistency
11. Policy

`approved` and `released` are not statuses.

## 7. RBAC and disclosure

- The tool needs `view_quote`. A caller without it (for example `manufacturing_engineer`)
  gets `error` / `forbidden` and no draft. Revoked or unauthenticated principals fail closed.
- RBAC v2 `view_quote` covers both internal cost and selling price; no finer permission
  exists, and none is invented. A5 therefore keeps internal cost out of its result entirely,
  and the customer projection is selling prices only. The A5 result itself is an
  **internal** review record: it holds review reasons, rule fingerprints and evidence digests.
- Error results never carry exception text.

## 8. A1–A4 limitations preserved

| Limitation | A5 handling |
|---|---|
| A1: customer text is not authority | No free-text input; customer `part_id` never enters explanations |
| A1: clarifications | `clarification_required` with the fields; nothing invented |
| A2: evidence unsigned | Re-validated by the A4 replay (digest binding + A2's checks). Integrity is consistency-checked, not authenticated |
| A2: rule drift / infeasible | `requires_review` / `not_evaluated`; never quoted |
| A3: separate snapshots, no atomic transaction | A5's quotation call is a fourth snapshot; disagreement is `invalid_evidence`, never merged |
| A3: estimate, not commitment | `estimated_ready_date` is labelled "a planning estimate, not a delivery commitment"; no commitment field |
| A3: constrained, RFQ-level only, no partial orders | `supply_status` kept; `supply_constrained` review reason; one RFQ-level draft, never split; capacity limits keep `estimated_ready_date = None` |
| A3: delivery provenance | `requested_delivery_date` is A3's verified Intake value; feasibility is A3's |
| A4: `priced` is not approval | A5 output is a draft needing human approval |
| A4: pricing tools report no snapshot ids | A4 vs A3 snapshot identity is not claimed; the quotation cross-check compares outcomes, not snapshots |
| A4: drift suppresses amounts; raw evidence has amounts | A4 `requires_review` → A5 `requires_review` with no lines, no total and no evidence copied (tested) |
| A4: `view_quote` covers cost and price | No cost in A5 output; customer projection is selling-only |
| A4: no tax, freight, shipping or discounts | `excludes = (tax, freight, shipping, discounts)`, stated in the explanation |
| A4: rates not independently re-derived | Unchanged: A5 does not re-derive rates either |

## 9. Evidence validation and errors

`QuotationEvidence` keeps:

- the arguments digest (equal to A3's)
- A4's arguments digest
- the validated `QuotationOutput` (selling amounts only)

Unverifiable tool results are not kept: an `error` result has `evidence=None`.

A forged, tampered, swapped or stale A4 or A3 is caught by the A4 replay and returns
`not_evaluated` / `pricing_assessment_invalid` with no tool call. A forged A4 includes, for
example, changed totals, dropped review reasons, a hidden stock status or promoted drift
amounts. The A3 or A4 assessment of another specification is also caught this way.

## 10. Limitations and future integration

- **No persistence**: no draft id, version, idempotency key or approval request. These need
  an authorized, idempotent draft-write service and a human-only approval API. Both are out
  of the Phase scope.
- **No quote validity/expiry**: no business rule defines a validity period. The
  `quote_versions.expires_at` column exists but has no rule. A validity rule must be
  approved and registered before A5 can report one, together with a trusted clock input.
- **Customer standing and credit unevaluated**: no agent tool exposes the customer's account
  status, credit limit or exposure. This needs the deferred commercial-policy tool
  (`evaluate_stored_rfq` under `approval-v1.2`, server-pinned) and approval to add it.
- **No recorded human decision is read**: this needs a read-only approval-status tool.
- **Snapshot drift between calls** shows up as `invalid_evidence` (fail closed), not as a
  merged result. Re-run the chain.
- Upstream evidence is consistency-checked, not cryptographically signed.
