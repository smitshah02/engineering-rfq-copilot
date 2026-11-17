# Supervisor clarification and escalation

Module: `src/rfq_copilot/escalation.py`. Integration: `supervisor._execute` (trusted `build`)
and `SupervisorExecutionResult.disposition` / `customer_outcome()`.

## 1. Authority and data flow

```text
A1..A5 results ─> record_outcome ─> StageOutcome ─> decide() ─> RouteDecision
                                                         │
             custody (this attempt only) ─ codes ─┐      │
                                                   v      v
                     build(): esc.for_result(route, None, draft?, A2 findings, A5 unevaluated)
                                                   │
                                     SupervisorDisposition (internal, codes)
                                                   │
                                      customer_view() -> CustomerOutcome (fixed text)
```

- `decide()` stays the only router, and no node was added. The disposition is a pure,
  one-to-one reading of the settled route. `SupervisorExecutionResult` now refuses a `route`
  that is not `decide(outcomes)`, and a `disposition` that is not
  `for_result(route, error, draft_available, its findings, its unevaluated codes)`.
- The only inputs not on the result are two code tuples that `build` reads from this
  attempt's custody:
  - A2's finding codes of its infeasible parts (only for a `not_quotable` stop);
  - A5's `unevaluated_review_conditions` (only when a draft exists).

  Both are checked against the agents' own vocabularies (`a2._KNOWN`; A5's
  `CUSTOMER_CONDITIONS` plus `borderline_bend_radius`).
- **Authenticity (D48-02).** `run_supervisor_workflow` is the authenticated construction
  boundary: it builds `blocking_findings` and `unevaluated_conditions` from this attempt's
  custody, and custody is cleared afterwards. A serialized result is consistency-checked, not
  authenticated. The model independently enforces:
  - the route equals `decide(outcomes)`, and every other disposition field follows from it;
  - a draft carries exactly one of A5's two unevaluated-condition shapes
    (`A5_UNEVALUATED`: account status and credit limit always, plus `borderline_bend_radius`
    optionally), so the unchecked customer conditions can never be removed, reordered or
    replaced;
  - `not_quotable` carries at least one A2 finding, unique, in A2's vocabulary, and never a
    `MISSING_*` code.

  It **cannot** tell, after serialization, *which* allowed A2 finding produced the stop, or
  whether A5 really added the optional bend condition. Those are tested as documented limits.
  Signed evidence is L04/L21/L26.
- `status="executed"` (technical: the run settled) and the category (business) stay
  separate. There is no success flag.

## 2. Categories

| Route | Route outcome | Category | Customer outcome |
|---|---|---|---|
| `clarification` | `clarification_required` | `clarification_required` | `information_needed` |
| `blocked` | `not_quotable` (A2 infeasible) | `not_quotable` | `not_quotable` |
| `blocked` | `not_evaluated` | `not_evaluated`: could not be evaluated, **not** an impossibility | `unable_to_evaluate` |
| `review` | `requires_review` | `review_required` | `under_review` |
| `complete` | `draft_ready_for_review` | `draft_ready_for_approval` | `draft_pending_approval` |
| `failed` | `error` | `technical_failure` | `technical_issue` |
| (no route: run failed before settling) | — | `technical_failure`, `stage=None` | `technical_issue` |

The flags `clarification_required`, `blocked`, `review_required` and `technical_failure` are
validated against the category. Constant fields: `automatic_continuation=False`,
`human_approval_required=True`, `approved=False`, `released=False`.

## 3. Clarification

- Codes are the route's (specialist workflow `_clarifications`): Intake ids `"<part>:<field>:<kind>"`,
  A2 `"<part>:<spec field>:missing"`, and A3
  `"0:requested_delivery_date:requested_delivery_date_unverified"`.
- Questions come from fixed templates per kind:
  - `missing`, `ambiguous`, `contradiction`, `ownership_unverified` and `invalid` combine
    with an allow-listed field label (Intake's `_LABELS`, plus A2 field names; dimensions
    say "in millimetres");
  - `unknown_operation`, the delivery-date code and `no_parts` have fixed sentences.
- A part is named only by its validated index, never by its customer-written label.
- A code that does not fully match the pattern, or has an unknown field or kind, gets
  `GENERIC_QUESTION`.
- Questions are deduplicated, kept in the route's order, and capped at `MAX_QUESTIONS` (25).
- Intake's own `Clarification.question` is **not** reused, because it quotes customer text.
- Nothing runs downstream (the route stopped). Nothing is resumed: answering is a new attempt
  that re-validates everything (recovery owns interruption and resumption).

## 4. Blocking

- `not_quotable` exists only at `manufacturing`. `blocking_findings` holds A2's codes,
  internal only. `blockers` holds customer-safe categories: material, finish, thickness,
  part type, tolerance, bend radius, operation, outside supported range, or `other` when
  nothing explainable is present.
- No substitute material, operation or partial fulfilment is suggested.
- `not_evaluated` is blocked but reported as "could not be evaluated". It has no blockers
  and never says the request is impossible.
- A tool error is `technical_failure` (route `failed`), never a blocker.

## 5. Review recommendations

| Category | Codes (where they come from) |
|---|---|
| `engineering` | `manufacturing_requires_review` (A2 rule drift), `BORDERLINE_BEND_RADIUS` (A5 policy), `intake_{engineering_review, unsupported_requirement, unknown_operation, catalog_no_match, catalog_ambiguous}` |
| `supply_delivery` | `supply_requires_review` (A3), `supply_constrained` (A4), `DELIVERY_DATE_INFEASIBLE`, `CAPACITY_OUT_OF_HORIZON`, `CAPACITY_DATA_MISSING` (A3/A5 lead-time findings) |
| `commercial` | `pricing_requires_review`, `pricing_rule_drift`, `pricing_contradicts_upstream` (A4/A5), `QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD` (A5 manager review) |
| `intake` | other `intake_<kind>` review findings (catalog unavailable or not checked, rules not checked, injection suspected) |
| `other` | any unknown code; also a review with no code |

- Constrained supply still continues to pricing (specialist workflow `_NEXT`). Its concern surfaces at
  A5, and that sequence is unchanged.
- Categories are recommendations only. Nobody is assigned, no ticket is created, nothing is
  notified or approved.
- A completed draft keeps any A5 manager-review reasons as categories. The customer sees
  only "awaiting approval".

**Customer status:** no tool or service verifies account standing or credit. A5 lists
`customer_account_status` and `credit_limit` as unevaluated. The disposition therefore always
has `customer_status="not_verified"` and carries A5's codes in `unevaluated_conditions`. It
is never a pass, and never a block on its own. An authoritative account and credit service
is a quotation lifecycle requirement (L08–L17).

## 6. Draft

`draft_ready_for_approval` requires `draft_available`. A draft may also accompany
`review_required` at `quotation`. The draft itself stays A5's `customer_view()`, unchanged.
The customer outcome says that a non-binding draft awaits approval. It is not a quote, and it
is not released, persisted, numbered or sent.

## 7. Technical failure

`failed` routes and unsettled runs carry their fixed code in `reason`, which is operator-only:

- handoff and provenance refusals;
- `timeout`, `internal_error`, `unauthenticated` and untrusted rules;
- `execution_limit`.

The customer gets one fixed sentence ("No decision has been made"). There are no questions,
blockers or review notices, and no retry.

## 8. Projections

- **Internal** (`disposition`, included in `summary()`): category, stage, reason codes,
  route codes, questions and correlation through the result's `attempt_id` and identity.
  There are no amounts or evidence. Raw A4 and A5 results stay in custody and are cleared.
- **Customer** (`customer_outcome()`): outcome name, fixed message, questions, blocker
  sentences, review notices (review only) and `draft_available`. There are no codes,
  amounts, customer text, exception text or rule diagnostics.
- `view_quote` does not open internal costing. Neither projection contains any (L27/L28).
