# Authentication, authorization and human decisions

The ordinary and decision APIs verify asymmetric JWTs. Subjects resolve to active PostgreSQL
identities; database grants and eligible customer bindings create Principals. Customer roles
have narrow `customer_*` permissions and no staff-costing MCP or decision grants. Staff reads
add a trusted operational role check; service identities do not become staff by sharing a grant.

## Current policy and process boundaries

API permission policy v3 maps customer and staff operations separately. Registered RBAC v4
includes the customer overlay and internal workflow service role; environment flags do not
create grants. Assignment enforcement is configurable and applied in SQL. MCP independently
requires policy v2, revocation and a distinct token audience.

The current schema contract is revision `20261118_01`, retaining one active customer binding
per user. Multi-customer staff changes remain deferred. The development worker signs short-lived
MCP service tokens through `DevMcpCredentialProvider`; production issuance is still outstanding.

Serving factories load only the selected restricted credential, never the owner URL. Ordinary
API/MCP, worker, drafter and decision processes keep separate authority. Human decisions check
current grants/bindings, exact version, policy and evidence. Approvers differ from the creator
and requester; releasers differ from the approver. Approval context attestation is explicit.
No verified engineering-review evidence means refusal, not an administrative bypass.

## Route permission policies

`ApiSettings` defaults to legacy policy v1; the local launcher explicitly selects v3.
Policy selection does not register permissions. The database must contain the reviewed
overlay and grant rows. Under v3, staff retain the v2 route grant and the following
customer grants are accepted for customer operations:

| Operation | Legacy v1 | Staff v2/v3 | Customer alternative under v3 |
| --- | --- | --- | --- |
| Customer profile | `view_customer` | `view_customer` | `view_customer` |
| RFQ reads / attachment list | `view_assigned_rfqs` | `view_assigned_rfqs` | `customer_view_rfq` |
| Material catalog / optional specification route | `view_assigned_rfqs` | `view_catalog` | None |
| Released quotation read / discovery | `view_assigned_rfqs` | `view_quote` | `customer_view_quote` |
| Attachment upload | `create_rfq` | `attach_rfq_document` | `customer_attach_rfq_document` |

Customer RFQ creation separately requires `customer_create_rfq` and the service's active
customer identity checks. Staff quotation reads require `view_assigned_rfqs` plus a trusted
operational staff role. Review submission requires `request_approval`; decision permissions
are `approve_quote`, `reject_quote`, and `release_quote`. Administrative roles do not imply
cross-customer access or a separation-of-duties bypass.

Configure revocation, upload idempotency and assignment enforcement explicitly. The local
template enables revocation/idempotency and leaves assignments off; required assignments
must actually be provisioned and aligned across REST and MCP. MCP always requires its
dedicated audience, policy v2 and revocation. Customer accounts receive no catalog-costing
MCP grants. The API's separate hardened profile requires production mode, policy v2,
revocation and upload idempotency; it is not the v3 customer-demo profile or production
deployment acceptance.



## Authentication and identity resolution

<a id="authentication-trust-configuration"></a>
### Trust configuration

Configure API settings through process variables; the application does not search for a
dotenv file. Set the JWT trust fields together or leave them unconfigured. An unconfigured
trust boundary refuses protected bearer requests with 503; invalid partial settings fail
application construction. Public liveness and main-API system metadata are separate.

| Setting | Contract |
| --- | --- |
| `RFQ_API_JWT_ISSUER` | Exact trusted issuer |
| `RFQ_API_JWT_AUDIENCE` | Exact API audience |
| `RFQ_API_JWT_ALGORITHM` | `RS256`, `ES256`, or `EdDSA` |
| `RFQ_API_JWT_PUBLIC_KEY_FILE` | Absolute PEM public-key path; alternative to a key set |
| `RFQ_API_JWT_PUBLIC_KEYS` | One to four `kid=/absolute/path` entries for rotation; alternative to one key file |
| `RFQ_API_JWT_MAX_LIFETIME_SECONDS` | Maximum accepted declared lifetime; default 900 seconds |
| `RFQ_API_TOKEN_REVOCATION` | `off` by default or explicit `enforced` |

Verification checks signature, configured algorithm, issuer, exact audience, required
integer `iat`/`exp`, lifetime, applicable `nbf`, allowed headers and a well-formed subject.
There is no token-selected algorithm, remote key fetching, private-key loading or HMAC
fallback. A configured key set requires a known `kid`; keys are not tried indiscriminately.

Public-key files and their directory chain are checked through trusted descriptors for
ownership/permissions, regular-file type and symlink safety. RSA keys require at least
2048 bits. This filesystem boundary is POSIX-specific; unsupported key layouts are refused.
See [tokens.py](../../src/rfq_copilot/api/tokens.py) and
[paths.py](../../src/rfq_copilot/paths.py).

<a id="authentication-request-identity-flow"></a>
### Request identity flow

1. Accept only `Authorization: Bearer ...`; cookies, query parameters and spoofed identity
   headers do not establish a caller.
2. Verify the token before database access. Bad/missing credentials receive 401.
3. Resolve its subject to an active `business.users` row.
4. If revocation is enforced, check token digest and user revocation state. Unknown
   revocation/database status fails closed rather than assuming a token is valid.
5. Resolve an eligible authoritative customer binding. No eligible context receives 403.
6. Read allowed role permissions from `business.role_permissions`, then build the immutable
   `Principal`. Token role/customer/permission claims grant nothing.
7. Enforce the route permission and customer/assignment scope in its queries.

Identity is re-resolved on subsequent requests rather than cached as an authorization
snapshot. See [auth.py](../../src/rfq_copilot/api/auth.py) and
[the authorization guide](authentication.md).

<a id="authentication-customer-context-and-mcp"></a>
### Customer context and MCP

The active schema permits one active customer binding per user. Selectable REST customer
context is prepared for a deferred schema contract; it is not permission to activate
multi-customer access. MCP independently resolves a single customer context.

REST credentials are never forwarded to MCP. A trusted server-side provider must obtain a
separate MCP-audience token for the same subject, and the gateway verifies it independently
with revocation enforced. The worker supplies a development-only MCP service-token provider; production delegation remains outstanding. See [delegation](../../src/rfq_copilot/api/mcp_integration.py).

<a id="authentication-human-decision-boundary"></a>
### Human-decision boundary

Approval, rejection and release run in a separate FastAPI process using the same verified
identity model plus the restricted K5 decision role. Request bodies cannot choose actors,
roles or customers. Services recheck grants/bindings and version freshness; database guards
also enforce authorized lifecycle transitions and separation of duties.

Only that process receives the decision credential. Neither an authenticated browser nor
an agent receives database URLs or signing/decision secrets. Serving configuration selects only restricted process authority; see the [limitations register](technical_debt.md).

<a id="authentication-provisioning-and-verification"></a>
### Provisioning and verification

Use a trusted issuer and reviewed operator tooling to provision subjects, bindings and
revocation state; seeding does not create runtime customer bindings. Configure public keys
without copying signing keys into the repository or printing credentials in commands/logs.

Relevant tests include `tests/test_auth.py`, `tests/test_auth_database.py`,
`tests/test_api_security.py`, and `tests/test_k5_decision_authority.py`. HTTP decision and database regression coverage is implemented; see the verification guide. See [testing](testing.md) for executable verification scope and isolation requirements.


## Authorization, customer scope and human decisions

<a id="rbac-authorization-customer-and-assignment-isolation"></a>
### Customer and assignment isolation

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

<a id="rbac-authorization-persistent-quotation-and-human-authority"></a>
### Persistent quotation and human authority

Draft creation and revision are Python services. Staff HTTP adapters expose scoped stored
quotation reads, action assessment and review submission; human decisions use the separate
decision application. RBAC does not replace deterministic business
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
