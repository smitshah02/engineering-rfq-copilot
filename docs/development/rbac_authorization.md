# Authorization, customer scope and human decisions

Authentication identifies the user; database grants authorize an operation; scoped queries
limit which records it can access. Each check is required. This guide retains its path for
existing references and describes current policy rather than development-day chronology.

## Roles and customer representation

The current roles are internal staff/operators: `sales_representative`, `estimator`,
`manufacturing_engineer`, `sales_manager`, `administrator`, `auditor`, and `identity_operator`.
A verified user acts for an eligible bound customer. Administrators have no implicit
cross-customer access or human-approval bypass.

A distinct external-customer role/policy does not exist. Do not give customer accounts a
staff role merely to make a demonstration work. In particular, `view_quote` also authorizes
internal MCP costing; it is not a released-price-only customer permission.

The active K5 schema keeps one active customer binding per user. The proposed multi-customer
migration remains [deferred](../../alembic/proposed/README.md).

## Main API permission matrix

The selected policy comes from `RFQ_API_PERMISSION_POLICY`; v1 is the default. Permissions
are read from database grants, never inferred from role claims or browser-supplied fields.
The reviewed `rbac-policy-v2` overlay must actually be provisioned before its grants exist.

| Operation | Policy v1 | Policy v2 |
| --- | --- | --- |
| Customer profile | `view_customer` | `view_customer` |
| RFQ reads | `view_assigned_rfqs` | `view_assigned_rfqs` |
| Attachment metadata | `view_assigned_rfqs` | `view_assigned_rfqs` |
| Material catalog/specification | `view_assigned_rfqs` | `view_catalog` |
| Released quotation read | `view_assigned_rfqs` | `view_quote` |
| Attachment upload | `create_rfq` | `attach_rfq_document` |

`require_route_permission` resolves this operation matrix per request. A verified user
without the required grant receives 403 before route work. Protected session dependencies
require identity; missing/bad credentials receive 401, and unavailable trust/database
checks fail closed. See [auth.py](../../src/rfq_copilot/api/auth.py).

## Customer and assignment isolation

`Principal.customer_id` comes from an eligible database binding. RFQ/quotation/attachment
queries filter ownership in SQL. Missing, foreign and unassigned RFQs are indistinguishable
404 responses. Shared material-catalog identities are intentionally global.

With `RFQ_API_RFQ_ASSIGNMENTS=required`, RFQ-scoped operations also require the verified
user's active assignment. With assignments off, customer scope still applies. Align the
chosen assignment policy across REST and MCP; a client header cannot grant an assignment.
MCP independently re-resolves identity and requires permission policy v2 and revocation.

The customer quotation route returns only released quotation fields selected by the
[response schema](../../src/rfq_copilot/api/schemas/reads.py). Internal cost breakdowns,
margins, raw evidence, and unreleased quotations are not part of that response.

## Persistent quotation and human authority

Draft creation, revision, assessment and submission are Python services; their complete
operator HTTP adapters are not built. RBAC does not replace their deterministic business
checks, expected-version checks, transaction boundaries or immutable history.

The separate [decision application](../../src/rfq_copilot/api/decision_app.py) exposes:

| Route suffix under `/api/v1/quote-decisions/{quote_id}` | Required permission |
| --- | --- |
| `/approval` | `approve_quote` |
| `/rejection` | `reject_quote` |
| `/release` | `release_quote` |

Each request carries the expected version and idempotency key, never an actor/customer/role.
Approval also requires explicit context attestation; rejection requires a reason. The service
re-resolves active actor/grants/bindings, locks and reassesses the exact current version,
and requires the restricted decision role. The approver differs from creator/requester;
the releaser differs from the approver. Required unverified engineering review is refused.

K2/K3/K4/K5 guards enforce the corresponding database invariants. Keep the
[K2 runtime](../database/k2_runtime_roles.sql) and [K5 decision](../database/k5_decision_roles.sql)
provisioning scripts and pinned migration files intact. Agents and generic MCP tools have
no approval/release authority. Hiding a button is not an authorization control.

## Verification and remaining integration

The permission matrix, real JWT flow, scoped queries and lifecycle protections have
regression modules including `tests/test_rbac.py`, `tests/test_day21_rbac_database.py`,
`tests/test_quote_approval_database.py`, and `tests/test_k5_decision_authority_database.py`.
Shared fixtures and schema/provisioning documents are test dependencies.

Pending operator review/version/history APIs, customer-role design, concurrent decision
retry behavior and authenticated decision-HTTP coverage are recorded in the
[current limitations register](technical_debt.md). This documentation update did not run
those tests or change grants, policies, schemas or application behavior.
