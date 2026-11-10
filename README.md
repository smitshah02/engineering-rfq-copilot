# Engineering RFQ Multi-Agent Copilot

An engineering request-for-quotation (RFQ) copilot for a fictional sheet-metal
manufacturer. The design is a supervisor agent and five specialist agents. They turn customer
RFQs into internal draft quotations, and every customer-facing release is approved by a
human.

The business logic is deterministic Python over PostgreSQL. The AI layers (agents, the
tool server and the API) come later and will call these services; they will not make the
decisions themselves.

## Status

| Phase | Days | State |
| --- | --- | --- |
| 1: Architecture, dataset, database | 1–5 | Complete |
| 2: Deterministic business services | 6–15 | Complete |
| 2.5: Hardening checkpoint | n/a | Complete; see [the hardening report](docs/development/phase_02_5_hardening_report.md) |
| 3: API foundation (FastAPI app, liveness, system metadata) | 16 | Complete; see [the API foundation notes](docs/development/day_16_fastapi_backend.md) |
| 3: API contracts (Pydantic request/response schemas, structured 422 errors) | 17 | Complete; see [the API validation notes](docs/development/day_17_pydantic_api_validation.md) |
| 3: Protected read APIs (customer, RFQs, materials, released quotes); fail-closed identity | 18 | Complete; see [the read API notes](docs/development/day_18_database_rest_apis.md) |
| 3+: Authentication/RBAC, write APIs, tool server (MCP), agents (LangGraph), persistence, review UI | 19–73 | **Not started** |

### What works today

- **PostgreSQL schema and data layer.** 33 tables from one Alembic migration, async
  SQLAlchemy ORM models, seeding from dataset v1.1.0 with integrity checks, and read
  repositories that are scoped to a customer.
- **Deterministic services** (`src/rfq_copilot/services/`):
  - material catalog checks and manufacturing-capability validation
  - inventory availability
  - first-fit weekly capacity planning
  - lead-time estimation
  - manufacturing cost
  - pricing (exact `Decimal`, `calculation_id`)
  - multi-part quotation
  - business evaluation: the outcome, the escalation reason, the draft gate and manager
    review
- **Provenance.**
  - Every result records the rule versions and fingerprints it used.
  - Each calculation reads exactly one inventory snapshot and one capacity snapshot.
  - Rule documents are checked against a version-controlled trusted manifest.
  - Each evaluation carries an `evaluation_fingerprint` over all of its inputs and results.
- **HTTP API foundation** (`src/rfq_copilot/api/`): a FastAPI application factory with
  process liveness and service metadata only. It exposes no business endpoint, reads no
  database and needs neither the dataset nor database credentials. See [API](#api).
- **API contracts** (`src/rfq_copilot/api/schemas/`, `api/errors.py`): Pydantic v2 request
  and response schemas for future RFQ endpoints and a structured HTTP 422 error body.
  No route uses the RFQ submission schema yet.
- **Protected read APIs** (`api/routers/{customers,rfqs,materials,quotes}.py`): read-only,
  customer-scoped reads through the existing repositories. They need a verified identity,
  and none exists yet, so every one of them answers 401 in a real deployment.
- **RFQ attachments** (`api/routers/attachments.py`, `src/rfq_copilot/attachments.py`):
  PDF and CSV bill-of-materials upload to an RFQ the caller owns. The content is checked,
  the text or BOM rows are extracted deterministically, the file is stored under a
  server-generated name, and metadata goes into `rfq_attachments`. Also identity-gated,
  so it is 401 in a real deployment.

### Planned, not implemented

None of the following exists yet:

- write API endpoints (RFQ submission, evaluation, quotation) and API authentication
- the MCP tool server
- the LangGraph supervisor and agents
- quote-draft persistence
- execution of approval requests or decisions
- the human-in-the-loop flow
- the frontend

The services decide; they do not write, reserve, approve or release anything.

## Architecture

```
            (planned)                                  (implemented)
 Customer RFQ ─► Intake agent ─┐
                               ├─► Supervisor ─► tool server ─► evaluate_rfq
 Engineering / Supply / Pricing│                                  │
 / Quotation agents ───────────┘                                  ▼
                                   select_snapshots (one inventory + one capacity, dated)
                                                                  │
                    rules (trusted manifest) ──► manufacturing ─► inventory ─► capacity
                                                                  │
                                                     lead time ─► costing ─► pricing
                                                                  │
                                         quotation ─► customer & policy gates ─► RfqEvaluation
                                                                  │
                                       (planned) human review ─► human approval ─► human release
```

There are six agents: the Supervisor (A0) and five specialists: Intake, Engineering,
Supply & Scheduling, Pricing, and Quotation & Review. Their roles are defined in
[docs/architecture/agent_responsibilities.md](docs/architecture/agent_responsibilities.md).

## Security and approval boundaries

- **Agents never release or approve quotations.** `quote_release_authorized_for_agent` is
  always `False`. Any approval policy that would let an agent approve or release a quote,
  or that drops the human-release requirement, is rejected
  (`services/evaluation.py::require_human_release`).
- **Rule documents are tamper-evident.** A rule loads only if its stored source hash and
  its document fingerprint both match `repositories/rule_manifest.py`. Unregistered
  versions are refused.
- **Customer scope.** RFQ, quotation and approval reads require the verified
  `customer_id`, and they return nothing for another customer's records. Authenticating
  that ID is the job of the future tool server.
- **Read only.** Business calculations only read. Tests write only inside the guarded
  `*_test` database, in transactions that are rolled back.
- **Secrets.** `.env` is ignored by Git and is never printed.

## Dataset (private)

**Decision (2026-11-09):** the complete dataset v1.1.0 stays private. It is not committed,
and `.gitignore` excludes it:

```
/data/rfq_synthetic_data/
/data/rfq_synthetic_data_v1.1.0.zip
```

Authorized developers receive the archive separately and install it as described in
[Fresh install](#fresh-install), step 3. A public clone runs only the smoke suite (see
[Testing](#testing)).

Dataset v1.1.0 is 100 % synthetic. A scan found no secrets, credentials or local paths:
- every email address is on a reserved `.example` domain;
- every phone number is a fictional `555-01xx` number;
- PDF metadata names only the fictional manufacturer.

It stays private mainly because the archive contains the held-out evaluation split and its
answers. Those answers sit in more places than the frozen folders:

| Must stay private | Why |
| --- | --- |
| `evaluation/ground_truth/frozen_evaluation/` | The frozen answer keys |
| `rfq_scenarios/frozen_evaluation/` | The held-out test inputs |
| `evaluation/agent_coordination_cases.json`, `evaluation/failure_scenarios.json` | Contain expected outcomes for frozen RFQs |
| `evaluation/scenario_manifest.json`, `evaluation/checksums.sha256.json` | Frozen family/category assignments and label hashes |
| `src/`, `config/generation_config.yaml`, `tests/` (inside the dataset) | The seeded generator rebuilds every frozen label exactly |
| `rfq_synthetic_data_v1.1.0.zip` | Contains all of the above |

**Deferred (TD-008):** publishing a sanitized, development-only subset. Seeding verifies
the complete archive (its SHA-256 and all 637 protected hashes, frozen files included), so
a subset needs its own approved verification profile first. It also needs a licence; the
dataset has none.

**Inventory check.** Seeding requires the extracted files to match the archive's 680
files exactly. Any extra, missing or changed file fails. The one exception is an
OS-metadata file named exactly `.DS_Store`, which is ignored in any folder.

## Fresh install

This sequence was run end to end from a clean copy of the repository on 2026-11-09. It
used the test target, and the development target is identical apart from the names.

**1. PostgreSQL.** The safety checks accept only:
- PostgreSQL 18 on loopback port 5432;
- databases named exactly `rfq_copilot_dev` and `rfq_copilot_test`, UTF-8 and owned by
  the connecting role;
- a role without SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS.

Run this once as an administrator (choose your own role name and password):

```sql
CREATE ROLE rfq_copilot_dev LOGIN PASSWORD '<choose one>';
CREATE DATABASE rfq_copilot_dev  OWNER rfq_copilot_dev ENCODING 'UTF8' TEMPLATE template0;
CREATE DATABASE rfq_copilot_test OWNER rfq_copilot_dev ENCODING 'UTF8' TEMPLATE template0;
```

**2. Python and configuration.**

```bash
uv python install 3.12
uv sync --locked
cp .env.example .env && chmod 600 .env
# Fill in DATABASE_URL (rfq_copilot_dev), TEST_DATABASE_URL (rfq_copilot_test) and
# TEST_DATABASE_NAME=rfq_copilot_test.
```

**3. Dataset** (authorized users only):

```bash
# Place the archive at data/rfq_synthetic_data_v1.1.0.zip and keep it there: seeding re-checks it.
shasum -a 256 data/rfq_synthetic_data_v1.1.0.zip
#   expected: 681a882fd7c22cd4e874b57fd31ab6bd12602bc826b2c02cfa79eb17a5ad5080
unzip -q data/rfq_synthetic_data_v1.1.0.zip -d data/      # creates data/rfq_synthetic_data/ (680 files)
chmod 700 data/rfq_seed_snapshots                         # a clone creates it as 0755; seeding requires 0700
```

Never modify the extracted files.

**4. Development database: migrate, seed and register.**

```bash
P=.venv/bin/python
D=(--source data/rfq_synthetic_data --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env)
$P -m rfq_copilot.database --env-file .env --target development                     # read-only connectivity check
$P -m alembic -x target=development -x env_file=.env upgrade head                    # 20261108_01 -> 20261109_01
$P -m alembic -x target=development -x env_file=.env current --check-heads           # 20261109_01 (head)
$P -m rfq_copilot.seeding.cli --target development "${D[@]}" --dry-run                      # PASS, read only
$P -m rfq_copilot.seeding.cli --target development "${D[@]}" --apply --confirm-database rfq_copilot_dev   # inserted 1089
$P -m rfq_copilot.seeding.rules approval-v1.2 --target development --env-file .env --confirm-database rfq_copilot_dev  # "registered"; re-run: "already present"
```

**5. Verify** (both commands are read only):

```bash
$P -m rfq_copilot.seeding.cli --target development "${D[@]}" --verify      # PASS, inserted 0, already_existing 1089
$P -m rfq_copilot.integrity --target development "${D[@]}"                 # PASS, records 1090 (1089 baseline + approval-v1.2)
```

**6. Test database.** Leave `rfq_copilot_test` **empty**: do not migrate or seed it. The
test fixtures refuse to start if it already contains tables. They migrate and seed it
inside each test module, roll back, and downgrade it again afterwards.

Seeding is described in
[docs/development/database_seeding.md](docs/development/database_seeding.md).

### Approval-rule versions

Registering `approval-v1.2` adds one row and changes nothing else:
- `evaluate_rfq` and `evaluate_stored_rfq` take `approval_rules` as a required keyword
  argument with no default;
- no code looks up a "latest" version;
- every result records `approval_rule_version`, its fingerprint and an
  `evaluation_fingerprint`.

A caller must load the version it wants by name with
`get_rule_version(session, "approval", "approval-v1.1" | "approval-v1.2")`. That call
refuses any version or content that is not in `repositories/rule_manifest.py`. The
future tool server must pin the version per request and must not build a
`RuleVersionDetails` itself.

## Testing

| Command | Needs | What it proves |
| --- | --- | --- |
| `uv run pytest -q -p no:cacheprovider tests/test_database_config.py tests/test_hardening_public.py tests/test_api.py tests/test_api_schemas.py tests/test_api_reads.py tests/test_attachments.py` | nothing | **Smoke only.** Configuration safety, canonical fingerprints, the planning-date policy, the Intake boundary, the API foundation, the API contracts, the protected read routes and attachment intake (no database). CI runs this ([.github/workflows/smoke.yml](.github/workflows/smoke.yml)). |
| `.venv/bin/python -m pytest -q` | dataset, `.env`, PostgreSQL | **Full regression.** Reference and ground-truth parity, the database lifecycle and every service (1111 tests at Phase 2.5; see the latest dev log for the current count). |
| `.venv/bin/ruff check .` and `.venv/bin/ruff format --check src tests alembic` | nothing | Lint and format |
| `uv lock --check` | nothing | The lockfile is current |

Most test modules read the frozen dataset when they are imported. Database tests fail,
rather than skip, when credentials are missing. The smoke suite is not end-to-end
verification.

Without the private dataset, run only the smoke suite. The full suite needs the dataset,
`.env` and both databases.

## API

The API is a foundation only: two read-only endpoints, no authentication, no database.

```bash
RFQ_API_ENVIRONMENT=development uv run uvicorn --factory rfq_copilot.api.app:create_app --host 127.0.0.1 --port 8000
```

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/health/live` | `{"status": "alive"}`: the process is serving. Not a readiness check; nothing downstream is checked. |
| GET | `/api/v1/system/info` | `{"name", "version", "api_version"}` |
| GET | `/docs`, `/redoc`, `/openapi.json` | Interactive docs and schema, **development only** |
| GET | `/api/v1/customers/me` | The verified customer's profile. **401 without verified identity** |
| GET | `/api/v1/rfqs?limit&offset` | The verified customer's RFQs, newest first. **401** |
| GET | `/api/v1/rfqs/{rfq_id}` | One of the verified customer's RFQs; another customer's is 404. **401** |
| GET | `/api/v1/materials?category&grade&thickness_mm&finish_id&limit&offset` | Catalog identities, no cost data. **401** |
| GET | `/api/v1/quotes/{quote_id}` | A quote a human has **released** to the verified customer; any other quote is 404. **401** |
| POST | `/api/v1/rfqs/{rfq_id}/attachments` | Multipart, one `file` part (PDF or CSV BOM). 201 returns metadata and the extraction. Rejections are 400/413/415/422 with a fixed `reason`; another customer's RFQ is 404. **401** |
| GET | `/api/v1/rfqs/{rfq_id}/attachments` | Attachment metadata of one of the verified customer's RFQs (no download route). **401** |

There is no authenticator yet: `api/auth.py::get_principal` always answers 401 and reads
nothing from the request. Integration tests inject a verified principal with a FastAPI
dependency override. Reads use a lazily created, read-only `Database` built from the process
`DATABASE_URL`; a database failure is an opaque 503. Database tests:
`.venv/bin/python -m pytest -q tests/test_day18_api_database.py` (needs `.env` and PostgreSQL).

Configuration comes only from `RFQ_API_*` process variables; no dotenv file is read.
`RFQ_API_ENVIRONMENT` is `production` (the default: docs and schema return 404) or
`development`. Any other value, or an unknown `RFQ_API_*` variable, stops startup.
`RFQ_API_ATTACHMENT_DIR` (an absolute path; uploads answer 503 until it is set) and
`RFQ_API_MAX_UPLOAD_BYTES` (default 10 MiB, at most 50 MiB) configure attachment uploads.
Details: [docs/development/day_19_file_uploads_extraction.md](docs/development/day_19_file_uploads_extraction.md).
Tests: `uv run pytest -q tests/test_api.py` (needs nothing). Details:
[docs/development/day_16_fastapi_backend.md](docs/development/day_16_fastapi_backend.md).

**Contracts (not yet routed).** `api/schemas/rfq.py` defines `RfqSubmissionRequest` and
`PartSpecification` (shaped like the service `Part` contract), `RfqReceipt` and
`QuoteCalculationSummary`. Validation is structural only: types, wire formats, unknown
fields, precision and duplicate `part_id`s. Exact numbers are JSON integers or plain
decimal strings, never floats, and money is serialized as an exact string. Manufacturing,
catalog, pricing and approval decisions stay in the services, so a valid request can still
be unsupported. The body carries no customer identity: `customer_id` is rejected.
Any request that fails validation returns HTTP 422:
`{"code": "REQUEST_VALIDATION_ERROR", "message": "Request validation failed.", "details":
[{"field", "code", "message"}]}`. Submitted values are never echoed. Tests:
`uv run pytest -q tests/test_api_schemas.py` (needs nothing). Details:
[docs/development/day_17_pydantic_api_validation.md](docs/development/day_17_pydantic_api_validation.md).

## Safe demonstration

After you have installed the dataset and seeded the development database:

```bash
.venv/bin/python -m rfq_copilot.database --env-file .env --target development   # read-only connectivity check
.venv/bin/python -m pytest -q tests/test_day15_evaluation.py                     # all 160 dev/validation RFQs, test DB, rolled back
```

## Current limitations

- `borderline_bend_radius` is evaluated only under `approval-v1.2` (borderline means within
  25 % above the minimum radius). Under `approval-v1.1` it is reported in
  `unevaluated_review_conditions` instead.
- Customer-requested operations (`requested_operations`) are refused at the `Part`
  boundary until the Intake contract supports them (TD-001).
- There are no live freshness checks against the wall clock. Those belong at the future
  API boundary.
- The dataset is private. A public development-only subset is deferred (TD-008).
- The API has no authentication, so its protected read routes always return 401 outside
  tests. It has no readiness check; its only write is the identity-gated attachment
  upload, and no live RFQ intake exists. Markdown attachments, OCR and attachment download
  are not implemented.

Full list: [docs/development/technical_debt.md](docs/development/technical_debt.md).

## Repository structure

- `src/rfq_copilot/`: models, repositories, seeding, integrity checks, services and the API foundation (`api/`)
- `alembic/`: migrations (`20261108_01` initial schema, `20261109_01` capacity key per snapshot)
- `tests/`: unit, integration and hardening tests
- `docs/`: architecture, data, database, development logs and reports
- `data/`: the private frozen dataset (ignored by Git)
