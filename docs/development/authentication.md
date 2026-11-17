# Authentication and identity resolution

The REST and human-decision FastAPI applications verify externally issued bearer JWTs.
They do not issue tokens or implement login, refresh or logout endpoints. The historical
filename is retained for existing links; prior implementation/test chronology is private.

## Trust configuration

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

## Request identity flow

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
[the authorization guide](day_21_rbac_authorization.md).

## Customer context and MCP

The active K5 schema permits one active customer binding per user. Selectable REST customer
context is prepared for a deferred schema contract; it is not permission to activate
multi-customer access. MCP independently resolves a single customer context.

REST credentials are never forwarded to MCP. A trusted server-side provider must obtain a
separate MCP-audience token for the same subject, and the gateway verifies it independently
with revocation enforced. No runtime credential provider is shipped; test providers do not
establish production readiness. See [delegation](../../src/rfq_copilot/api/mcp_integration.py).

## Human-decision boundary

Approval, rejection and release run in a separate FastAPI process using the same verified
identity model plus the restricted K5 decision role. Request bodies cannot choose actors,
roles or customers. Services recheck grants/bindings and version freshness; database guards
also enforce authorized lifecycle transitions and separation of duties.

Only that process receives the decision credential. Neither an authenticated browser nor
an agent receives database URLs or signing/decision secrets. Current serving configuration
still has owner-credential isolation limitations; see the [limitations register](technical_debt.md).

## Provisioning and verification

Use a trusted issuer and reviewed operator tooling to provision subjects, bindings and
revocation state; seeding does not create runtime customer bindings. Configure public keys
without copying signing keys into the repository or printing credentials in commands/logs.

Relevant tests include `tests/test_auth.py`, `tests/test_day20_auth_database.py`,
`tests/test_phase3_hardening.py`, and `tests/test_k5_decision_authority.py`. HTTP decision
success-path/database coverage remains follow-up work. The historical regression baseline
is in the [README](../../README.md#verification); it was not rerun for documentation cleanup.
