# Day 20: Authentication and JWT Security

Status: 2026-11-10. **Complete.** Verified identity plus an authoritative customer binding (TD-012, approved and resolved by revision `20261110_01`; see the last two sections). Uncommitted. The development database was migrated with authorization; full regression 1583 passed.

## Decision

| Question | Finding in the repository | Consequence |
| --- | --- | --- |
| Credential login possible? | No password, hash or credential column anywhere. `users.authentication_subject` is a "fictional opaque ID" from an external provider (dataset data dictionary). | No login endpoint and no password hashing (Argon2 does not apply). Tokens come from one configured external issuer. |
| Who can authenticate? | `business.users` (24 internal users; roles sales_representative … auditor), `UNIQUE (authentication_subject)` and `active`. | `sub` is resolved to an active user row. |
| Subject-to-customer relationship? | None. `users` has no customer column; `contacts` has no subject; the dataset's security oracle takes customer scope from session context. | `Principal.customer_id` cannot be derived. Access fails closed with 403 (TD-012). |

The schema and models are unchanged. A proposed migration is in `technical_debt.md` (TD-012) and is waiting for approval.

## Architecture

| Module | Role |
| --- | --- |
| `api/config.py` | Adds `jwt_issuer`, `jwt_audience`, `jwt_algorithm` (`RS256`, `ES256` or `EdDSA`), `jwt_public_key_file` and `jwt_max_lifetime_seconds` (default 900, range 60–3600). The first four must be set together or not at all. |
| `api/tokens.py` | `TokenVerifier` (PyJWT 2.15.1 with cryptography 50.0.2) and `load_public_key`. |
| `repositories/identity.py` | `find_active_user(session, subject)` and `customer_binding(session, user)`. The binding returns `None` because no relationship exists. |
| `api/auth.py` | `get_principal`, now real, with the same signature and the same `Principal`. Routes are unchanged. |
| `api/app.py` | Builds the verifier at startup. A bad key stops startup. |

### Trust boundary (configuration only)

- One issuer, one audience, one algorithm and one PEM public key file. HMAC is excluded: the service only verifies tokens, so it holds no signing secret.
- Key checks at startup:
  - The key must be a public key (a private key is refused) of the configured type.
  - RSA keys need at least 2048 bits; ES256 keys must use P-256.
  - The key file is read through descriptors, so the file checked is the file read:
    - its parent directory is opened and checked with `fstat` (owned by root or the service user, not group- or world-writable);
    - the file is opened relative to that directory with `O_NOFOLLOW` (a symlink fails) and `O_NONBLOCK` (a FIFO cannot block startup);
    - `fstat` on the opened descriptor requires a regular file owned by root or the service user, not group- or world-writable;
    - at most 16 KiB + 1 byte is read, and anything over 16 KiB is refused.
  - Error messages name no path and no key material.
- Nothing in a token can change the boundary:
  - The header's `alg` must equal the configured algorithm, and `jwt.decode` also receives that one algorithm.
  - Only the headers `alg`, `typ` (`JWT` or `at+jwt`) and `kid` are allowed. `kid` is ignored.
  - `jku`, `jwk`, `x5u`, `x5c` and `crit` are refused. No key is ever fetched.

### Verification rules (`TokenVerifier.subject`)

- **Shape:** at most 4,096 ASCII characters and exactly two dots. This is checked before any decoding.
- **Required claims:** `exp`, `iat`, `iss`, `aud` and `sub`. `nbf` is enforced when present.
- **Issuer and audience:** must match exactly. `strict_aud` means a list-valued `aud` is rejected.
- **Times:** 30 s leeway. `iat` must not be in the future. `exp - iat` must not exceed `jwt_max_lifetime_seconds`. `iat` and `exp` must be integers (booleans and floats are rejected).
- **Subject:** 1–255 characters, no surrounding whitespace, and no control, format or line-separator characters. It is kept verbatim.
- **Output:** only the subject. Customer, role, scope and email claims are never read.
- **Failure:** a single `InvalidToken` with no reason.

### Request lifecycle (`get_principal`)

1. `HTTPBearer(auto_error=False)` reads only the `Authorization` header. Missing, empty or non-Bearer credentials get 401 with `WWW-Authenticate: Bearer`. Cookies, query parameters, `X-*` headers and the body are never read.
2. If no trust boundary is configured, the answer is 503 `{"detail": "Service unavailable."}`. Nothing is authorized, and requests without credentials still get 401.
3. The token is verified. Any failure gets 401 before any database access; tests count SQL statements and get zero.
4. A short session is opened on the read-only `Database` (`get_database`, called directly). The route's own session dependency itself depends on `get_principal`, so it cannot be used here, which also avoids a circular dependency. The lookup is `SELECT … FROM business.users WHERE authentication_subject = :sub AND active`. If it finds nothing, the answer is 401, identical to a bad token. A database failure gets an opaque 503. The session is closed before the route runs.
5. `customer_binding(session, user)` returns `None` today, so the answer is 403 `{"detail": "Forbidden."}`. Once a binding exists, `Principal(customer_id)` comes only from the database, and the existing customer-scoped SQL applies unchanged.

The protected routes (customers/me, rfqs, rfqs/{id}, materials, quotes/{id}, and GET/POST rfqs/{id}/attachments) keep their dependencies. An upload with a rejected token stops at step 1 or 3: no body is read, no storage directory is created and no write database is built. `/health/live` and `/api/v1/system/info` stay public and ignore any token.

## Authentication versus authorization

Day 20 answers "who is this?": a verified token for an active user. Day 21 RBAC must decide what that user may do. It must:
- define how an internal user acts for a customer (TD-012), as an approved table, not as a token claim;
- read the user's role from `business.users.role` and `role_permissions`, never from the token;
- keep released-only quote visibility and the existing customer-scoped SQL;
- keep agents unable to approve or release (`AGENT_FORBIDDEN` in the dataset oracle).

## Token provisioning

An external identity provider issues short-lived tokens. Each must carry:
- `iss` equal to `RFQ_API_JWT_ISSUER` and `aud` equal to `RFQ_API_JWT_AUDIENCE`;
- `sub` equal to the user's `business.users.authentication_subject`;
- integer `iat` and `exp` with a lifetime of at most `RFQ_API_JWT_MAX_LIFETIME_SECONDS`;
- a signature from the private key that matches `RFQ_API_JWT_PUBLIC_KEY_FILE`.

Operators provision the public key file (mode 0600 or 0644, owned by root or the service user) in a directory owned by root or the service user that is not group- or world-writable (for example `/etc/rfq-copilot/`, 0755 root).

**Deployment assumptions (key file):**
- Directories above the key's parent are not checked. They must be writable only by root (or the service user), as system directories normally are. Symlinked ancestors such as macOS `/etc` → `/private/etc` are followed.
- The configured path must name the file itself. Symlink-based layouts, such as Kubernetes secret volumes (`key.pem` → `..data/key.pem`), are refused; point the setting at the resolved file instead.
- An attacker running as root or as the service user is out of scope.
- The key is read once at startup; replacing it afterwards has no effect until restart (TD-013).
- POSIX only (`dir_fd`, `O_NOFOLLOW`), like the rest of the deployment. Disabling a user (`active = false`) blocks their tokens on the next request.

**Key rotation (TD-013):** a single static key. Rotation means replacing the file and restarting; a short lifetime limits exposure. Revocation before `exp` works only through user deactivation.

## Tested attacks

**Public, `tests/test_auth.py` (87 tests):**
- Malformed, empty and oversized tokens (oversized ones are never decoded).
- Bad signatures, a tampered payload and a flipped signature.
- Expired tokens, a future `nbf` or `iat`, and a lifetime over the limit.
- Wrong and near-miss issuer, wrong audience, and a list audience.
- Each required claim missing; boolean and float times.
- Hostile subjects: blank, padded, newline, bidi, non-string, too long.
- `alg` values `none`, `None` and `NONE`, and a token with no `alg` at all.
- HS256 signed with the server's RSA public key (algorithm confusion).
- Tokens using the other allowed families, ES384 and PS256.
- `jku`, `x5u`, `crit`, a wrong `typ`, and an embedded attacker `jwk`.
- Privilege claims being ignored.
- Partial, weak, wrong-type, private, writable, symlinked and missing key configurations.
- The key path swapped for a symlink to an attacker's key after the loader's first check (deterministic `lstat()`/`open()` race; the pre-correction loader accepted the attacker's tokens).
- A foreign-owned key file, a group- or world-writable or sticky `1777` directory, a FIFO, a directory, and a 16 KiB limit enforced on the bytes read; each of RS256, ES256 and EdDSA loads and verifies.
- Missing, empty and non-Bearer credentials; spoofed `X-*` headers, cookies and query parameters.
- Invalid tokens producing zero lookups.
- Unknown or inactive users answered like bad tokens.
- The verified-but-unbound 403.
- The customer coming only from the binding.
- Unconfigured trust (503) and an identity database failure (503).
- Public endpoints, anonymous upload with no storage or write database, 401 before 422.
- 30 interleaved concurrent requests, each getting its own identity and one session.
- The OpenAPI bearer scheme without issuer or path.

**PostgreSQL, `tests/test_day20_auth_database.py` (7 tests)**, with real tokens, the seeded `business.users` and no identity override:
- A real active user gets 403 on all 7 operations with exactly one `business.users` SELECT each and no writes.
- Forged customer, role and scope claims get 403.
- Unknown and disabled users get 401.
- The subject match is exact (case, suffix, `%`).
- Invalid tokens issue zero SQL statements.
- Pipeline readiness, with only `customer_binding` stubbed and clearly labelled: scoped profile, cross-customer RFQ 404, unreleased quote 404, own upload 201 and a foreign upload 404, and the sanitized 422 behind real authentication.

**Mutation checks:**

| Mutation | Result |
| --- | --- |
| Signature verification disabled | 4 failed |
| User lookup bypassed | 3 failed |
| Customer taken from the token | 3 failed |
| Enforcement removed | 10 failed |
| Header `alg` check and `algorithms=` allowlist removed together | 2 failed |

Either algorithm layer alone still blocks the attacks; the two layers are deliberate defence in depth.

## Verification (actual results)

| Check | Result |
| --- | --- |
| `pytest tests/test_auth.py tests/test_day20_auth_database.py` | 81 + 7 passed |
| Day 16–19 API tests (6 files, including the database tests) | 364 passed |
| Public smoke (7 files, no `DATABASE_URL`) | 465 passed |
| Full `pytest -q` | **1563 passed**, 0 failed, 0 skipped, 0 warnings (baseline 1475) |
| `ruff check .` / `ruff format --check src tests alembic` | pass / 109 files formatted |
| `uv lock --check` | pass. The lock adds only pyjwt 2.15.1, cryptography 50.0.2, cffi 2.1.1 and pycparser 3.12 |

**Uvicorn, production, configured ES256 trust, development DB read-only lookup.** Results on all 7 protected operations:

| Request | Result |
| --- | --- |
| No token | 401 |
| Bad signature | 401 (`WWW-Authenticate: Bearer`) |
| Unknown subject | 401 |
| Valid token for a real active user | 403 |
| Spoofed headers, cookie and query | 401 |

Also: `/health/live` 200, 0 error lines, the token not in the log.

**Uvicorn, misconfigured:**
- No trust configured: bearer gets 503, no credentials get 401.
- Partial trust configuration: startup fails ("JWT settings must be given together").
- World-writable key: startup fails, and the path is not logged.

**Integrity:** `python -m rfq_copilot.integrity --target development` (read only): PASS, 1090 records.

## Limitations

- **TD-012 (blocker):** no customer binding, so no customer data is reachable through real authentication.
- **TD-013:** single static key; no JWKS or revocation list.
- **TD-009 (still open):** the identity lookup uses the read-only `Database`. An injected one is still not verified read-only, but the lookup only reads.
- TD-010, TD-011 and the Day 19 storage limitations are unchanged.

## TD-012 resolution: authoritative customer binding (approved 2026-11-10)

### Migration design (`alembic/versions/20261110_01_user_customer_bindings.py`)

`business.user_customer_bindings`:

| Column | Type | Rule |
| --- | --- | --- |
| `user_id` | TEXT NOT NULL | FK `users(user_id)`, RESTRICT; identity check |
| `customer_id` | TEXT NOT NULL | FK `customers(customer_id)`, RESTRICT; identity check |
| `created_at` | TIMESTAMPTZ NOT NULL | default `CURRENT_TIMESTAMP` |
| `revoked_at` | TIMESTAMPTZ NULL | null means active; must be ≥ `created_at` |

- **Keys and indexes:** primary key `(user_id, customer_id)`; partial unique index `uq_user_customer_bindings_active_user (user_id) WHERE revoked_at IS NULL`, so a user has at most one active binding (the single-customer `Principal`); index `ix_user_customer_bindings_customer_id`.
- **Stable ids, not subjects:** the table references the database user id. The authentication subject stays only in `users`, so changing a subject never orphans a binding.
- **Additive only:** no existing table, column or data changes. No ORM relationships were added, so `User` and `Customer` are unchanged.
- **Downgrade:** refuses while any binding row exists, so authoritative records are never dropped silently. Tested on the guarded test database.
- **Contract pins updated:**
  - `seeding/safety.py` pins head `20261110_01` and shape digest `3c9f9e44…`, measured on the guarded test database (upgrade, digest, guarded downgrade, tracker cleanup; the database is empty again afterwards).
  - `seeding/mapping.py` lists the table as a runtime table (never seeded).
  - The `integrity.py` catalog counts are updated.
  - The ORM and migration contract tests record TD-012 as an explicit post-Day-4 addition, next to TD-005.

### Binding resolution (`repositories.customer_binding`)

The lookup selects the customer of the user's unrevoked binding, joined to `customers` with `account_status` in the eligible set, `LIMIT 2`. It returns a customer only for exactly one row.

- **Eligible statuses:** `active`, `pending_verification`, `on_hold`, `credit_restricted`. These are the CHECK constraint values minus `inactive`; a test ties the set to the constraint.
- **Why those statuses:** `business_rules.md` uses the other statuses to gate commercial review and release. The business services still enforce that, so they don't block sign-in.
- **Fail closed:** missing, revoked, `inactive`, unknown status or ambiguous gives `None`, and the API answers 403. A disabled user is refused earlier with 401.
- **Never inferred:** email, name, region, RFQ authorship and token claims are not used.

### Request lifecycle, now complete

1. Bearer JWT.
2. Signature and claims checks.
3. Active user by exact subject.
4. Exactly one eligible, unrevoked binding.
5. `Principal(customer_id)` from the database.
6. The existing customer-scoped SQL in the route's own session.

Steps 3 and 4 run in one short read-only session (two SELECTs).

### Tests (real signed JWTs, no `get_principal` override; bindings provisioned per test in the rolled-back fixture transaction)

`tests/test_day20_auth_database.py`, 18 tests:
- A bound user gets their profile, their RFQs only, materials, their released quotes only (every seeded quote checked), their own upload 201 and attachment listing. A forged claim naming another customer is ignored.
- Another customer's RFQ and a missing RFQ get identical 404s, and a foreign upload gets 404.
- Revoked binding: 403. Revocation takes effect on the next request.
- Account status: `active`, `pending_verification`, `on_hold` and `credit_restricted` get 200; `inactive` gets 403.
- A disabled user with a binding gets 401.
- Two users each get only their own customer.
- The schema refuses a second active binding and unknown user or customer ids; revoked history is allowed.
- Ambiguity fails closed even with the unique index dropped.
- An unbound user gets 403 with exactly two reads per request and no writes.
- Unknown and disabled users get 401. Invalid tokens issue zero SQL statements.
- The sanitized 422 is unchanged behind real authentication.

Also: `tests/test_database_migrations.py::test_td012_downgrade_refuses_to_drop_bindings_and_reverses_cleanly`, and a public status-set test in `tests/test_auth.py`.

**Mutation checks on `customer_binding`:**

| Mutation | Result |
| --- | --- |
| Revocation ignored | 2 failed |
| Account status ignored | 1 failed |
| First row of an ambiguous match taken | 1 failed |
| User filter dropped | 1 failed |

### Verification (actual results)

| Check | Result |
| --- | --- |
| Day 20 tests (`test_auth.py`, `test_day20_auth_database.py`) | 82 + 18 passed |
| TD-012 downgrade guard (guarded test DB) | passed; test DB empty afterwards |
| Public smoke (7 files, no `DATABASE_URL`) | 466 passed |
| Full `pytest -q` | 1580 passed, **3 failed**, 0 skipped, 0 warnings |
| `ruff check .` / `ruff format --check src tests alembic` | pass / 110 files formatted |
| `uv lock --check` | pass (no dependency changes) |
| `alembic current` (development) | `20261109_01`, deliberately not migrated |
| `integrity --target development` | FAIL: "Alembic revision is not current" (expected until migrated) |
| Uvicorn against un-migrated development | live 200; no token 401; valid token 503 (opaque; nothing logged) |

The 3 failures all read the **development** database, which correctly lacks the table until it is migrated:
- `test_current_development_integrity_and_preservation`, whose expectations were updated to values measured on the seeded test database;
- `test_development_physical_schema_read_only`;
- `test_all_development_reads_preserve_records_and_close_sessions`.

**To finish, with your authorization:**

```bash
.venv/bin/python -m alembic -x target=development -x env_file=.env upgrade head
```

Then rerun the 3 tests and `python -m rfq_copilot.integrity --target development …`.

## Development migration (authorized 2026-11-10)

**Preflight**
- Target `rfq_copilot_dev` on localhost, PostgreSQL 18.6. Revision `20261109_01`, code head `20261110_01`, bindings table absent.
- Offline `--sql` is disabled by `env.py` (live guards are mandatory). The DDL was instead rendered with Alembic's offline operations context: exactly `CREATE TABLE business.user_customer_bindings` plus its two `CREATE INDEX` statements, with no ALTER, DROP or DML on any other object.
- Backup: `pg_dump --format=custom` to `~/.rfq_copilot_backups/rfq_copilot_dev-pre-20261110_01-20261110T202936.dump` (outside the repository; directory 0700, file 0600; sha256 `3ea5e81e…`).
  - Verified with `pg_restore --list` (34 table-data entries: the 33 application tables plus `alembic_version`) and a full archive render to `/dev/null`.
  - Not restored into a separate database.
- Data fingerprints taken before the migration: an md5 of all rows plus a row count for each of the 33 tables (hashes only).

**Migration:** `alembic … upgrade head` ran `20261109_01 -> 20261110_01` (exit 0). `current --check-heads` reports `20261110_01 (head)`.

**Verification**

| Check | Result |
| --- | --- |
| New table | constraints pk, 2 fk, 3 checks; indexes `ix_user_customer_bindings_customer_id` and the partial unique `uq_user_customer_bindings_active_user`; 0 rows |
| Pre-existing tables | 33/33 fingerprints and row counts identical before and after the migration, and identical again after the full regression |
| Schema provenance comments | unchanged (`rfq-copilot:20261108_01`) |
| `integrity --target development` (read only) | PASS, records 1090, violations 0, unique data checks 18, populated tables 26, empty runtime tables 8 |
| The 3 previously failing development tests | 3 passed |
| Day 20 tests | 100 passed |
| Database, migration, ORM and seeding tests (6 files) | 555 passed |
| Public smoke (7 files, no `DATABASE_URL`) | 466 passed |
| Full `pytest -q` | **1583 passed**, 0 failed, 0 skipped, 0 warnings |
| `ruff check .` / `ruff format --check src tests alembic` / `uv lock --check` | pass / 110 files formatted / pass |

No bindings were created and no user was assigned. The guarded test database is empty after the run.

## Correction — Phase 3.5 (2026-11-10)

- Directories above the key's parent are now checked too (`rfq_copilot/paths.py`): each must be owned by root or the service user, and not writable by group or others unless sticky. The parent keeps its strict rule. This replaces the "not checked" deployment assumption above.
- Rotation (TD-013): `RFQ_API_JWT_PUBLIC_KEYS` holds 1 to 4 named keys; the token's `kid` must match one exactly. Revocation is TD-024.
- Still intentional, and not accepted on the user's behalf: symlinked key layouts are refused, POSIX only. See [phase_3_technical_debt_resolution.md](phase_3_technical_debt_resolution.md).
