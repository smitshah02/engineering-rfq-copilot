# Development Log: Day 15 — Business Service Integration

Date: 2026-11-09

## Objective

One deterministic business evaluation of an RFQ for a verified customer: the multi-part quotation (Days 7–14) combined with the customer's account status and credit position under the pinned approval policy. It returns the business outcome, the escalation reason, the draft-creation gate and the manager-review requirement. It decides and executes nothing: no persistence, no draft, no approval request or decision, no release. This closes Phase 2.

Baseline before editing: 948 passed (Day 14 final).

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/services/evaluation.py` | New: `CustomerStanding`, `RfqEvaluation`, `customer_action`, `review_threshold`, `evaluate_outcome` (pure), `evaluate_rfq`, `evaluate_stored_rfq`. |
| `tests/test_evaluation_service.py` | New: 21 database-free unit tests. |
| `tests/test_day15_evaluation.py` | New: 5 integration and parity tests on the guarded test database. |

No repository, ORM, migration, dataset, contract or dependency changes. The technical-debt register stays empty.

## Design

**Flow.** `evaluate_rfq(session, parts, *, customer_id, manufacturing_rules, pricing_rules, approval_rules, horizon_start, line_numbers=None, requested_delivery_date=None, contradictions=(), inventory_snapshot=None, capacity_snapshot=None)`:
1. Validates the approval rule kind and the line set before any SQL.
2. Reads the customer with Day 5 `get_customer`; returns `None` for an unknown customer.
3. Unless contradictions were supplied, runs Day 14 `quote_parts` once (Days 7–13 inside, each once). Contradictory specifications are not priced, as the dataset specifies.
4. Calls the pure `evaluate_outcome`.

`evaluate_stored_rfq(session, rfq_id, *, customer_id, ...)` uses the customer-scoped Day 5 `get_rfq`: `None` if the RFQ is absent or another customer's, `ValueError` without runtime lines.

**Rules.** All rules come from the pinned `approval-v1.1` document, with semantics taken from the dataset generator (read as a test reference only):
- **Customer action:** status in `blocked_customer_statuses` (`on_hold`, `inactive`) → `blocked_release`; in `commercial_review_customer_statuses` (`pending_verification`, `credit_restricted`) → `commercial_review`; `active` → `eligible`; anything else raises.
- **Credit breach** (only when priced): `credit_exposure_usd + quote_total_usd > credit_limit_usd`, strictly greater.
- **Outcome precedence:**

| # | Condition | Outcome |
| --- | --- | --- |
| 1 | contradictions, or a line needs clarification | `clarification_required` |
| 2 | a line cannot be manufactured | `unsupported_declined` |
| 3 | customer `blocked_release` | `customer_hold_blocked_release` |
| 4 | capacity infeasible, or delivery date infeasible | `engineering_review_required` |
| 5 | customer `commercial_review`, or credit breach | `commercial_review_required` |
| 6 | otherwise | `quote_draft_ready_for_approval` |

- **Escalation reason precedence (different from the outcome's):** `CONTRADICTORY_DOCUMENTS` > `MISSING_CRITICAL_SPECIFICATION` > `ENGINEERING_UNSUPPORTED` > `CUSTOMER_ACCOUNT_RESTRICTION` > `DELIVERY_DATE_INFEASIBLE` > `CREDIT_LIMIT_EXCEEDED` > `CAPACITY_OUT_OF_HORIZON` (> `CAPACITY_DATA_MISSING`). For example, a capacity-infeasible RFQ with a credit breach is `engineering_review_required` with reason `CREDIT_LIMIT_EXCEEDED` (RFQ-0062).
- **Manager review:** quote total ≥ the policy threshold (`7500.00`; the policy states it twice and both must agree), plus each condition in `manager_review_policy.also_required_for` (`credit_limit_breach`, `customer_not_active`). An unknown condition raises. The reasons are returned (`QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD`, `CREDIT_LIMIT_BREACH`, `CUSTOMER_NOT_ACTIVE`). A non-active customer needs review even when nothing was priced.
- **Draft gate:** `draft_creation_allowed` is true only when the quotation is priced, there are no contradictions and there are no lead-time findings. Commercial reviews and customer holds do not block drafting; they block release.
- **Release:** `human_release_approval_required` comes from the policy; `approval_must_be_human` is always true; `quote_release_authorized_for_agent` is false whenever the policy forbids the agent `release_quote` or requires human release, which is always the case here.

**Deliberate deviations and boundaries.**
- `CUSTOMER_ACCOUNT_RESTRICTION`: the generator keys it on its scenario category (`commercial`). At runtime there is no category, so the customer's non-eligible status decides. They agree on every labelled RFQ.
- `contradictions` are Intake's findings (Phase 3, from documents) and are an input here.
- Recovery scenarios label injected workflow faults (`SERVICE_FAILURE`, retries), which are orchestration concerns for Phase 3. For them only the business gates are compared, and the business outcome before the fault is asserted.
- The policy's `engineering_review_conditions` (for example `borderline_bend_radius`) are not used by the dataset's labels and are not enforced here.

## Commands and actual results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_evaluation_service.py` | 21 passed in 0.12s |
| `.venv/bin/python -m pytest -q tests/test_day15_evaluation.py` | 5 passed in 3.54s; after replacing a tautological distribution assertion with the exact one, 5 passed in 3.11s |
| `.venv/bin/python -m pytest -q` | **974 passed in 98.62s** (948 baseline + 26 new) |
| `.venv/bin/ruff check .` | All checks passed! |
| `.venv/bin/ruff format --check src tests alembic` | 71 files already formatted |
| `uv lock --check` | Resolved 23 packages |
| `git diff --check` | clean |
| dataset bytecode/cache scan | 0 files |
| `pytest tests/test_database_seeding.py::test_source_inventory_and_checksums` | 1 passed (681 files, 637 protected hashes) |

Parity coverage:
- **All 160 dev/validation RFQs**, evaluated with their labelled customer, requested delivery date and contradiction findings, the real snapshot pins and the sha-pinned `approval-v1.1`. All five gates (`draft_creation_allowed`, `manager_review_required`, `human_release_approval_required`, `approval_must_be_human`, `quote_release_authorized_for_agent`) match for all 160. Outcome and escalation reason match exactly for all 150 non-recovery RFQs. Exact distribution: 83 ready (73 labelled + 10 recovery before their fault), 40 clarification, 20 unsupported, 10 customer hold, 4 engineering review, 3 commercial review.
- **Live data:** raising a customer's exposure to one cent over the limit turns the same RFQ into `commercial_review_required` / `CREDIT_LIMIT_EXCEEDED`; setting the status to `on_hold` turns it into `customer_hold_blocked_release`.
- **Scope:** a stored RFQ evaluates with its real line numbers and the labelled calculation ID; another customer → `None`; an unknown customer → `None`; a wrong rule kind is rejected.
- **Unit:** threshold boundary (7499.99 / 7500.00), credit boundary (exactly at the limit is not a breach), both review statuses, both blocked statuses, unknown status, the escalation-precedence cases, contradictions, policy integrity (inconsistent thresholds, unknown review conditions).
- **Read only:** exactly 34 SELECTs for RFQ-0041 (customer and contacts 2, quotation 32); customer rows unchanged; repeat calls equal.

Read-only demo against development (`day15_demo.py` in the session scratchpad; the first development RFQ for each outcome and reason):

```
RFQ-0001 [CUST-008 active, exposure 8211.00 / limit 150000.00] total 2380.74
   -> quote_draft_ready_for_approval  reason None  draft True  manager review False  human release True  agent may release False
RFQ-0021 [CUST-028 active, ...] total None
   -> clarification_required  reason MISSING_CRITICAL_SPECIFICATION  draft False  manager review False
RFQ-0031 [CUST-018 active, ...] total None
   -> clarification_required  reason CONTRADICTORY_DOCUMENTS  draft False  manager review False
RFQ-0051 [CUST-038 active, ...] total None
   -> unsupported_declined  reason ENGINEERING_UNSUPPORTED  draft False  manager review False
RFQ-0062 [CUST-037 active, exposure 6228.00 / limit 25000.00] total 19577.64
   -> engineering_review_required  reason CREDIT_LIMIT_EXCEEDED  draft False  manager review ('QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD', 'CREDIT_LIMIT_BREACH')
RFQ-0065 [CUST-021 active, exposure 5460.00 / limit 25000.00] total 61436.79
   -> commercial_review_required  reason CREDIT_LIMIT_EXCEEDED  draft True  manager review ('QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD', 'CREDIT_LIMIT_BREACH')
RFQ-0066 [CUST-028 active, exposure 31671.00 / limit 150000.00] total 28344.44
   -> engineering_review_required  reason CAPACITY_OUT_OF_HORIZON  draft False  manager review ('QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD',)
RFQ-0069 [CUST-012 active, exposure 12903.00 / limit 150000.00] total 39524.83
   -> engineering_review_required  reason DELIVERY_DATE_INFEASIBLE  draft False  manager review ('QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD',)
RFQ-0071 [CUST-020 on_hold, exposure 22287.00 / limit 150000.00] total 189.91
   -> customer_hold_blocked_release  reason CUSTOMER_ACCOUNT_RESTRICTION  draft True  manager review ('CUSTOMER_NOT_ACTIVE',)
```
(Each line matched its label; the full output, with labels, is printed by the script.)

## Limitations

- Decisions only: draft persistence, approval requests and decisions, release, and the human-in-the-loop flow are outside Phase 2.
- The caller supplies the verified `customer_id`; authentication and role checks (`allowed_roles` in the tool contracts) belong to the Phase 3 tool server.
- Contradiction detection, workflow-fault recovery and the policy's `engineering_review_conditions` are not implemented here (see above).
- The credit check uses the customer's stored exposure; it does not add other open quotes.

## Phase 2 summary (Days 6–15)

| Day | Service | Ground-truth / reference parity |
| --- | --- | --- |
| 6 | `Part` entity, `get_rfq_parts`, verified `get_rule_version` | 180 extractions convert exactly; 9 rule documents match their source hashes |
| 7 | Material catalog (`materials.py`) | 180 parts; 1,024 reference combinations |
| 8 | Manufacturing validator (`manufacturing.py`), MFG-001 fixed | 180 findings, 120 routes; 243 reference changes |
| 9 | Inventory availability (`inventory.py`) | 100 RFQs, 120 part masses; 275 reference RFQs |
| 10 | Capacity planning (`capacity.py`) | 100 schedules (hours as exact strings); 104 reference cases |
| 11 | Lead time (`lead_time.py`), TD-002 | 100 ready dates and delivery findings; 182 reference RFQs |
| 12 | Manufacturing cost (`costing.py`), TD-003 | 120 cost lines; 360 reference lines |
| 13 | Pricing (`pricing.py`), TD-001 | 100 quotes including `calculation_id`; 183 reference quotes |
| 14 | Multi-part quotations (`quotation.py`) | 100 RFQs end to end, 20 multi-part |
| 15 | Business evaluation (`evaluation.py`) | 160 RFQs' business labels |

Tests grew from 595 (Day 5) to 974. All services are deterministic, read-only, `Decimal` end to end, pinned to sha-verified rule versions, and isolated from the frozen evaluation split.

## Git

Nothing staged, committed, pushed or stashed.
