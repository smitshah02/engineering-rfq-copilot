# RFQ_CoPilot — Manufacturing RFQ Platform

RFQ_CoPilot turns a manufacturing Request for Quotation into an engineering assessment
and a controlled customer quotation. It brings customer intake, feasibility checks,
supply planning, pricing, and staff review into one application, reducing manual
coordination while keeping commercial decisions under human authority.

**Status:** a complete local development application, demonstrated from customer
submission through automatic processing, independent approval, authorized release,
and customer quotation viewing with flat-panel and L-bracket RFQs. Production deployment
and operational acceptance remain outstanding.

## Customer and staff workflows

Customers can submit structured RFQs, add free-text requirements, upload PDF or CSV BOM
attachments, follow their RFQ history, and view quotations released to their account.
Internal drafts and approval information stay outside customer responses.

Staff can browse quotations in their authorized customer/assignment scope, inspect stored
manufacturing specifications and commercial totals, submit a version for review, and
perform permitted approval, rejection, or release actions. The server assesses available
actions and rechecks authorization, version, evidence, and policy when an action is confirmed.

1. **Submit:** the ordinary FastAPI service stores the RFQ, parts, idempotency receipt,
   audit event, and queued workflow in one transaction when automatic processing is enabled.
2. **Validate intake:** the worker loads the current persisted requirements and verifies
   part ownership, completeness, source evidence, catalog references, and contradictions.
3. **Evaluate:** a LangGraph Supervisor coordinates manufacturing feasibility, inventory,
   capacity, scheduling, pricing, and quotation review through authenticated MCP tools.
4. **Draft automatically:** a separate consumer claims eligible results and recomputes a
   deterministic internal quotation from the stored RFQ and trusted business rules.
5. **Review:** authorized staff submit the exact quotation version for independent human review.
6. **Approve and release:** an eligible independent approver records a context attestation;
   a separately authorized actor releases the approved version. The releaser must differ
   from the approver.
7. **View:** the customer discovers the released quotation through RFQ history and opens
   its selling-price projection.

Missing information, conflicting evidence, unsupported manufacturing requirements, or
required engineering review stop or redirect processing. A successful agent run alone
does not authorize a customer quotation.

## Architecture

```mermaid
flowchart TD
    Customer[Customer] --> Portal[React customer and staff portal]
    Staff[Authorized staff] --> Portal
    Portal --> API[Ordinary FastAPI service]
    API --> DB[(PostgreSQL: RFQs, queue, quotations, audit)]
    DB -- Claim queued work --> Worker[Continuous workflow worker]
    Worker --> Supervisor[LangGraph Supervisor and five specialists]
    Supervisor -- Unstructured extraction --> Ollama[Local Ollama model]
    Supervisor -- Principal-bound tool calls --> MCP[Authenticated MCP gateway]
    MCP --> Services[Deterministic manufacturing, supply and pricing services]
    Services --> DB
    Worker -- Settle evaluation --> DB
    DB -- Claim eligible outcome --> Drafter[Continuous quotation drafter]
    Drafter -- Recompute stored RFQ --> Services
    Drafter -- Persist internal draft --> DB
    Portal -- Human approval, rejection and release --> Decisions[Separate decision FastAPI service]
    Decisions --> DB
    API -- Released quotation projection --> Portal
```

### Supervisor and specialists

The Supervisor uses validated handoffs, deterministic routing, bounded parallel part
checks, clarification/escalation, and process-local evidence custody. Its five specialists are:

| Specialist | Responsibility |
| --- | --- |
| Intake and Validation | Extract or render requirements, verify provenance, resolve catalog references, identify missing or conflicting facts |
| Manufacturing Feasibility | Check material/process compatibility, dimensions, tolerances, finishes, and supported geometry |
| Supply and Scheduling | Assess inventory, procurement delay, workcentre capacity, and a feasible planning window |
| Pricing | Verify evidence and request deterministic costing and selling-price calculations |
| Quotation and Review | Check the combined assessment and identify policy or human-review requirements |

**Structured intake** uses the backend's current, customer-scoped persisted part fields
when there are no free-text notes. It renders exact values and source offsets without
asking a model to extract those fields again. These provisional facts still pass validation
and business checks. **Unstructured intake** uses local Ollama extraction; proposed fields
must be supported by their own source evidence. A worker-side SourceGate rejects invented
part identities and conflicts with stated persisted values before tool execution.

Manufacturing, inventory, capacity, lead-time, costing, and pricing results come from
versioned Python services using exact decimal arithmetic. Three MCP server families expose
ten read-only tools through an independently authenticated gateway. Model output cannot
set prices, change grants, or approve/release quotations.

See [specialist integration](docs/architecture/specialized_agent_integration.md),
[Supervisor delegation](docs/architecture/supervisor_langgraph_architecture.md),
[MCP architecture](docs/architecture/mcp_architecture.md), and
[tool contracts](docs/architecture/mcp_architecture.md).

### Continuous processing and persistence

PostgreSQL stores the durable processing queue. Independent workflow and drafting consumers
use row claims, leases, heartbeats, generation fences, bounded attempts, and idempotent writes.
The drafter accepts only eligible outcomes and re-evaluates persisted requirements instead
of accepting stored model conclusions as pricing authority.

Quotation versions, calculation provenance, review requests, decisions, releases, and
operation receipts are persisted with audit records. Queue recovery can repeat an interrupted
evaluation; process-local Supervisor evidence is not a durable continuation checkpoint.

The [background-processing guide](docs/development/background_processing.md) covers
configuration, startup, retry behavior, verification, and shutdown.

## Authorization and integrity

- Asymmetric JWT verification checks issuer, audience, signature, lifetime, and subject.
  Active identities, permissions, and customer bindings are resolved from PostgreSQL.
- Separate customer permissions restrict customer operations. Staff quotation reads also
  require a trusted staff role; customer scope and optional RFQ assignments are enforced in SQL.
- MCP has a dedicated token audience, database-derived grants, enforced revocation,
  typed contracts, and read-only evaluation transactions.
- The ordinary API, workflow worker, drafter, and human-decision service use separately
  restricted database authority. Owner credentials belong to reviewed administration.
- Submission, decisions, and release check the exact version and current policy.
  Approval requires separation from the quotation creator/requester, and release requires
  separation from the approver. Required unverified engineering review fails closed.
- Customer quotation DTOs select released selling-price fields explicitly. Browser
  identity changes clear cached data and reject stale responses from previous sessions.
- Database guards protect immutable history, calculation provenance, lifecycle transitions,
  and decision authority. Source hashes, rule fingerprints, schema contracts, and runtime
  integrity checks detect unexplained changes.

The current schema contract is defined in
[`schema_contract.py`](src/rfq_copilot/schema_contract.py), with active revision
`20261118_01`. Multi-customer staff migrations remain deferred. Consistency hashes do not
establish cryptographically signed specialist authorship.

See [authentication](docs/development/authentication.md),
[database architecture](docs/database/database_architecture.md), and the
[runtime](docs/database/k2_runtime_roles.sql),
[decision](docs/database/k5_decision_roles.sql),
[worker](docs/database/a1_workflow_role.sql), and
[drafter](docs/database/a4_drafter_role.sql) provisioning references.

## Technology and repository layout

| Area | Stack |
| --- | --- |
| Browser | React, TypeScript, Vite, React Router, TanStack Query, Tailwind CSS |
| API and persistence | Python 3.12, FastAPI, Pydantic, SQLAlchemy async, asyncpg, PostgreSQL 18, Alembic |
| Agent execution | LangGraph, LangChain Core, local Ollama, authenticated MCP |
| Engineering checks | Decimal calculations, versioned rules, provenance and integrity verification |
| Development verification | uv, pytest, Ruff, Vitest, Testing Library, GitHub Actions |

```text
frontend/              customer and staff portal, browser tests, development proxy
src/rfq_copilot/
  api/                 ordinary API, separate decision app, authentication, DTOs
  mcp/                 gateway, contracts, servers and client
  services/            deterministic engineering and commercial calculations
  models/              SQLAlchemy domain models
  repositories/        scoped queries and trusted rule loading
  seeding/             guarded private-source import and rule/policy registration
  policies/ / rules/   versioned authorization and business documents
  supervisor.py        specialist routing, handoffs and custody
  workflow_*.py        durable queue, evaluation worker and quotation drafter
  background.py        consumer polling, backoff and shutdown lifecycle
  quote_*.py           drafts, versions, evidence, review and human decisions
  *integrity.py        frozen baseline and explained runtime-state verification
alembic/               active and proposed/deferred migrations
scripts/dev_env.sh     process-specific local environment launcher
tests/                 backend regression, security, database and opt-in live checks
docs/                  architecture, database and operational references
```

## Local setup

Use Python 3.12, uv, PostgreSQL 18 on loopback, an installed Ollama model, and Node.js
matching `frontend/package.json` (`^20.19.0 || >=22.12.0`). The shell launcher and guarded
filesystem operations target a POSIX local environment. Run commands from the repository root.

```bash
uv sync --locked
npm --prefix frontend ci
# Preserve an existing private configuration.
test -e .env || (umask 077; cp .env.example .env)
```

The [environment template](.env.example) contains placeholders and non-secret local defaults.
Replace credential, key-path, and model placeholders privately. Serving processes read
process variables; the launcher loads only the selected process's settings. Do not export
the complete private file into every service or put secrets in browser configuration.

The synthetic dataset is private and supplied separately to authorized developers. The
full setup requires its frozen archive under `data/`, extracted sources under
`data/rfq_synthetic_data/`, and protected snapshot artifacts. Preserve all applied migrations,
registered rule bytes, manifests, and snapshots.

A fresh database additionally requires reviewed migrations, baseline seeding, current rule
and RBAC registrations, restricted roles, customer/staff/service identities and bindings,
any required assignments, and explicit queue activation. Dependency installation and
copying the template do not perform this provisioning. For an existing demonstrated
environment, reuse its approved configuration and state.

Consult [migrations](docs/development/local_development.md),
[seeding](docs/development/local_development.md), and the
[background-processing prerequisites and launch commands](docs/development/background_processing.md).
The current customer workflow selects API policy v3 with the registered RBAC v4 overlay.

| Local process | Default address |
| --- | --- |
| React portal | `http://127.0.0.1:5173` |
| Ordinary API | `http://127.0.0.1:8000` |
| Separate human-decision API | `http://127.0.0.1:8001` |
| MCP gateway | `http://127.0.0.1:8765` |
| Ollama | `http://127.0.0.1:11434` |

Start the two continuous consumers alongside these services using the operational guide.
The Vite development proxy sends ordinary API traffic and human decisions to their respective
processes. Local sign-in uses manually supplied, short-lived development JWTs for provisioned
identities; no production login or public registration service is provided.

## Verification

The [public smoke workflow](.github/workflows/smoke.yml) defines the dataset-free backend
selection and its lint/lock checks. It covers a subset of the regression suite. Frontend
tests use mocked HTTP requests; database and live integration checks have separate prerequisites.

| Manual command | Scope / prerequisites |
| --- | --- |
| `uv run pytest -q` | Full backend regression; private dataset, PostgreSQL, and guarded test credentials/roles required |
| `uv run ruff check .` | Python lint |
| `uv run ruff format --check src tests alembic` | Python formatting verification |
| `uv lock --check` | Dependency lock consistency |
| `npm --prefix frontend run test:run` | Frontend regression tests |
| `npm --prefix frontend run build` | TypeScript checking and production asset compilation |

Run database suites serially against the isolated `rfq_copilot_test` target; their fixtures
manage schema/data and must not share a concurrent test instance. Explicit development
integrity checks are read-only. Live Ollama, MCP, and optional Langfuse checks require
opt-in configuration. Offline or mocked tests do not establish live integration success.

### End-to-End Application Demo

These genuine screenshots show a successfully demonstrated local development workflow
using synthetic engineering RFQ data: one customer submission, automatic evaluation and
drafting, independent human approval, authorized release, and customer quotation viewing.

| Demonstrated example | Verified value |
| --- | --- |
| RFQ | `RFQ-1ba58316823e4733bede2e3ca1aa894e` |
| Quotation | `QUOTE-F9ADC0BBFE3640CF80551A61BFB82BC2` |
| Part / material / quantity | `P-TEST-BRACKET-003` · L-bracket · `MAT-001` · 20 units |
| Total / estimated lead time | **146.46 USD** · **4 business days** |
| Quotation version / final status | **1** · **Released** |

Images scale to the available width; open an image to inspect the original resolution.

### Actors and Separation of Duties

| Actor | Verified role/authority | Demonstrated responsibility |
| --- | --- | --- |
| Customer | Authenticated customer with account-scoped RFQ submission and released-quotation access | Submitted the RFQ once; viewed the final quotation after release |
| `USER-004` | `sales_manager`; granted `request_approval` and `release_quote`, subject to server checks | Submitted version 1 for review, then released it after USER-010's independent approval |
| `USER-010` | `sales_manager`; granted `approve_quote`, subject to server checks | Reviewed and explicitly approved version 1 independently; did not perform final release |

The staff role assignments and grants were verified against the synthetic demo identity
data. USER-004 and USER-010 are distinct authenticated identities with separately evaluated
permissions; their user IDs identify actors, while `sales_manager` is their assigned RBAC
role. Sharing a role does not authorize every action on every quotation.

**Human authorization is enforced by the backend.** It verifies the signed identity,
resolves current database grants and customer scope, and rechecks the quotation version,
evidence, policy, and separation of duties when an action is confirmed. USER-004 cannot
approve the quotation they submitted; USER-010 cannot release the version they approved.
Changing a displayed name or frontend button does not grant authority. See the
[authentication implementation](src/rfq_copilot/api/auth.py),
[decision enforcement](src/rfq_copilot/quote_approval.py), and
[existing lifecycle tests](tests/test_staff_quote_lifecycle_database.py).

### Customer Submission

The customer enters the L-bracket requirements and quantity in the RFQ form.

![Customer RFQ form with L-bracket part P-TEST-BRACKET-003, material MAT-001, and quantity 20](docs/screenshots/01-customer-rfq-form.png)

Submission succeeds once. The confirmation explains that a quotation becomes available
only after authorized review, approval, and release.

![Customer submission confirmation with the received RFQ and its L-bracket specifications](docs/screenshots/02-rfq-submitted-successfully.png)

### Automated Multi-Agent Evaluation

The continuous workflow worker picks up the queued RFQ. The LangGraph Supervisor
coordinates specialist evaluation through authenticated manufacturing, supply, and pricing
MCP requests; structured intake and business calculations use validated fields and
deterministic services. The worker log records successful requests and workflow completion.

![Workflow worker logs showing successful manufacturing, supply, and pricing MCP requests and completed evaluation](docs/screenshots/05-langgraph-mcp-workflow.png)

A separate background drafter consumes the eligible result, recomputes the quotation from
persisted requirements and trusted rules, and stores an internal draft automatically.

![Continuous quotation drafter logs showing work claimed and draft processing completed](docs/screenshots/04-automatic-quotation-drafter.png)

### Internal Quotation Draft

Staff inspect the generated version 1, its manufacturing specifications, total, and
estimated lead time. The draft is internal and non-binding until authorized release;
automatic processing grants no approval or customer visibility.

![Internal draft quotation showing version 1, 146.46 USD, four business days, and required human approval](docs/screenshots/07-quotation-draft-details-user-004.png)

### Human Review — USER-004

USER-004 submits the exact draft version for independent human review. This confirmation
requests review; it does not approve or publish the quotation. The requester cannot
independently approve their own submission, even with the `sales_manager` role.

![USER-004's review-submission confirmation for version 1, explicitly keeping the quotation unavailable to the customer](docs/screenshots/08-submit-for-human-review-user-004.png)

### Independent Approval — USER-010

USER-010 acts as the independent authorized approver, reviewing the manufacturing and
commercial details and explicitly confirming the exact quotation version and RFQ source
context. Approval applies to version 1 and leaves the quotation unavailable to the customer
until a separate authorized release. USER-010 did not perform that release in this demo.

![USER-010's independent approval confirmation with version 1, RFQ source digest, and explicit review attestations](docs/screenshots/11-human-approval-confirmation-user-010.png)

### Authorized Release — USER-004

After independent approval, USER-004 explicitly releases the approved version only when
the server authorizes the action. Release is a separate business action from approval:
it makes the quotation available to the authorized customer without sending an automatic
email or dispatching manufacturing work.

![USER-004's release confirmation for the independently approved version 1, explaining customer availability and no dispatch](docs/screenshots/14-quotation-release-confirmation-user-004.png)

### Customer Receives Released Quotation

The customer can now open the final **Released** quotation: version 1, **146.46 USD**, and
an estimated lead time of **4 business days**. Internal drafts, pending approvals, and
approved-but-unreleased quotations were unavailable to the customer before authorized
release; the customer API enforces this visibility boundary.

![Customer view of the released quotation with version 1, total 146.46 USD, four business days, and quantity 20](docs/screenshots/17-customer-released-quotation.png)

## Production limitations

- Local foreground processes require manual startup/restart. Deployment supervision,
  TLS, secrets distribution, monitoring, and operational acceptance remain outstanding.
- Development JWT issuance and the worker's local signing-key provider require replacement
  with a production identity and delegated-credential design.
- Restart recovery re-evaluates queued work; durable signed specialist evidence and
  cross-process continuation of trusted Supervisor custody remain incomplete.
- Inventory and weekly capacity checks provide planning estimates, without reserving
  stock, production slots, or shipment dates. Source freshness must be managed explicitly.
- Required engineering-review completion still needs verified evidence; approval refuses
  unsupported cases. Automatic expiry/supersession state transitions remain incomplete.
- The full regression and reproduction environment depend on separately supplied private
  data and reviewed database provisioning. Building frontend assets alone is not a deployment.

Detailed component limitations are recorded in
[technical debt and deferred decisions](docs/development/technical_debt.md).
