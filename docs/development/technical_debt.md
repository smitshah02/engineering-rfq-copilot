# Current Limitations and Deferred Decisions

This is the public maintenance register for the backend. It describes
current implementation boundaries, not an execution log or a production approval. Historical
bug investigations, remediation chronology, and test receipts are retained privately.

The [project overview](../../README.md) describes current capabilities and verification commands.

## Local demonstration and frontend integration

| Component | Current boundary | Follow-up |
| --- | --- | --- |
| Authentication | JWT verification, DB-derived grants, bindings, rotation and configurable revocation exist. Login/token issuance does not. | Integrate a trusted development issuer and define browser token/logout behavior. |
| Customer identities | Current roles are staff/operator roles acting for a bound customer. | Keep an initial demo internal; separately approve external-customer permissions. `view_quote` also permits internal MCP costing and is not a customer-only capability. |
| RFQ intake | Scoped reads and submission schemas exist; creation/correction HTTP routes do not. Baseline seeding creates no runtime RFQ lines. | Add a reviewed write service, requirement revisions, idempotency and narrow DB grants. The existing runtime INSERT allowlist excludes RFQs and their lines. |
| Delegated MCP credentials | A principal-bound credential-provider interface exists; runtime token exchange is not supplied. | Obtain separate MCP-audience credentials for the same verified subject. Never forward REST tokens or reuse a universal privileged identity. |
| Supervisor | A1–A5 execution, routing, clarification and escalation exist as Python components. | Add authenticated job-start, result, progress and cancellation APIs with bounded admission. No browser-supplied graph state, rules or specialist evidence. |
| Quotation services | Drafts, immutable revisions, assessment and submission are implemented. | Add thin authenticated write/read adapters and operator review/version/history APIs. |
| Human decisions | A separate FastAPI application exposes approval, rejection and release. | Add exact-version review context and browser routing that preserves the separate process; run the new authenticated HTTP/database tests manually. |
| Browser contracts | Decimal strings, aware timestamps and structured validation exist. | Complete multipart OpenAPI input, decision validation-error schemas and browser origin/proxy configuration. |

These gaps do not mean the underlying services are unimplemented. In particular, quotation
persistence, human decisions, the five specialists and the Supervisor are completed backend
components; their browser-facing integration is incomplete.

## Security and correctness follow-up

- **Serving credentials:** API/MCP factories select restricted application authority; the
  separate decision process selects decision authority. Neither serving configuration path
  reads owner credentials. Live role checks fail closed before yielding restricted sessions.
  Configuration and privilege-isolation tests await manual verification. See
  [database configuration](../../src/rfq_copilot/config.py).
- **Decision retries:** submission/approval/rejection now recheck receipts after serialized
  state changes, preserving payload conflicts and current authorization. Deterministic
  concurrent-request tests await manual execution. See
  [decision services](../../src/rfq_copilot/quote_approval.py).
- **HTTP assurance:** authenticated decision HTTP/service/PostgreSQL tests now cover the
  lifecycle, isolation, replay, rollback and forged-history rejection. They have not been
  executed for this remediation. The [public smoke workflow](../../.github/workflows/smoke.yml)
  includes dataset-free configuration and decision tests; it remains a subset.
- **Gateway ingress:** the configured MCP forwarding timeout does not cover all body
  reception. Bound slow request bodies before broader network exposure. See
  [gateway ingress](../../src/rfq_copilot/mcp/host.py).
- **Logging:** MCP wire errors are sanitized, while unexpected service/output errors can
  enter trusted exception logs. Review log redaction/export before centralized collection;
  this is distinct from the metadata-only Supervisor telemetry boundary.
- **Configuration:** REST policy v2, revocation, assignment enforcement and upload
  idempotency are deployment choices. MCP requires policy v2 and revocation. Provision
  actual grants/bindings and align policies rather than assuming configuration creates them.

## Deferred evidence and orchestration guarantees

- **L04/L21/L26 — durable evidence:** stored consistency hashes and calculation records
  do not authenticate specialist authorship. Signed durable specialist evidence is
  incomplete. Draft persistence recomputes from stored RFQ requirements, and human context
  attestation is required at binding approval; neither is cryptographic source authorship.
- **L05 — recovery:** continuation requires live process-local custody. A metadata record
  adopted after restart cannot reconstruct trusted specialist results. Cross-worker locking,
  durable continuation and exactly-once tool execution are not implemented. Use a single
  worker for a constrained local demonstration and report restart/loss behavior honestly.
- **Expiry/supersession:** validity is checked before binding approval/release, but automatic
  expiry and supersession state transitions are incomplete. A released historical record
  is not a promise that its validity date is still current.
- **Engineering review:** the decision service refuses required engineering review when
  verified completion evidence is unavailable. Do not bypass that refusal for a demo.
- **Multi-customer staff:** the active K5 contract retains one active customer binding per
  user. Proposed migrations remain [deferred](../../alembic/proposed/README.md); selectable
  REST context alone does not activate the schema change, and MCP remains single-customer.

## Deterministic service and data boundaries

- Explicit requested operation lists remain outside the `Part` contract (TD-001); tools
  derive supported routing from the pinned manufacturing rules rather than silently
  accepting an unmodelled list.
- Live planning freshness and source selection are application policy (TD-006). Binding
  drafts use a server-dated horizon; an exhausted capacity snapshot is refused. Do not
  modify frozen source data or choose an old horizon to make approval succeed.
- Read snapshot consistency and revalidation before persistence are different guarantees
  (TD-007). MCP evaluates in a read-only repeatable-read snapshot; quotation persistence
  and decisions recompute/check stored requirements and evidence in their write transaction.
- The synthetic dataset, operational snapshot bytes and frozen evaluation answers remain
  private. A clean public checkout supports the CI smoke selection; the full regression
  requires separately supplied private data and guarded test credentials.

## Production acceptance

Production identity provisioning, delegated credentials, monitoring, operational recovery,
deployment topology and security acceptance remain pending. Only explicitly exercised live
integrations establish live readiness; offline/model-mocked tests do not.

The active development contract is K5 (`20261116_04`). Keep its pinned migration history,
trusted business-rule files, schema documents, and [K2](../database/k2_runtime_roles.sql)/
[K5](../database/k5_decision_roles.sql) provisioning artifacts intact. Do not activate deferred
migrations, repin historical migrations, grant broader authority, or describe production
deployment as approved as part of documentation cleanup.
