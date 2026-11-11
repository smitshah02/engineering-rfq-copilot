# Engineering RFQ Multi-Agent Copilot

An AI-assisted copilot that turns engineering requests for quotation (RFQs) from
customers into accurate, auditable draft quotations for a sheet-metal manufacturer. A
human always approves and releases the final quotation.

The design separates **reasoning** from **deciding**. AI agents coordinate the work.
Every manufacturing, inventory, scheduling, pricing and approval decision comes from
deterministic, versioned Python services backed by PostgreSQL.

## The problem

Quoting a custom manufactured part is slow, and mistakes are expensive. For each RFQ, an
engineer has to:

- read the drawings and attachments;
- confirm the material, thickness and finish exist and are suitable;
- check the shop can actually make the part (bend radii, operations, tolerances);
- check inventory and plan capacity on the right workcentres;
- estimate the lead time, cost and price;
- route the quote through the right approval level before it reaches the customer.

Errors in any step lead to lost margin, missed delivery dates or quotes the shop cannot
honour.

## Intended workflow

1. **Intake.** The RFQ and its PDF or CSV bill of materials arrive and are validated.
   Part data is extracted.
2. **Engineering.** Materials and manufacturing capability are checked against the
   catalog and the rules.
3. **Supply and scheduling.** Inventory availability, capacity and lead time are
   computed from one consistent, dated snapshot.
4. **Pricing.** Costs and prices are calculated with exact decimal arithmetic.
5. **Quotation and review.** A multi-part draft quotation is assembled and evaluated
   against the approval policy. Reasons for escalation are recorded.
6. **Human approval.** An authorized person reviews, approves and releases the quote.
   Agents can never approve or release quotations.

## Architecture

```
 Customer RFQ ─► FastAPI (JWT · RBAC · customer isolation · attachments)
                        │
           (planned) LangGraph Supervisor ─► 5 specialist agents ─► MCP tools
                        │
        Deterministic engines: manufacturing ─► inventory ─► capacity
                               ─► lead time ─► costing ─► pricing ─► quotation ─► approval policy
                        │
        PostgreSQL (SQLAlchemy async · Alembic · versioned, SHA-256-pinned business rules)
                        │
                 Human review ─► human approval ─► human release
```

- **Agents (planned):** a LangGraph supervisor coordinating five specialists: Intake,
  Engineering, Supply & Scheduling, Pricing, and Quotation & Review. See
  [agent responsibilities](docs/architecture/agent_responsibilities.md),
  [the supervisor architecture](docs/architecture/supervisor_langgraph_architecture.md) and
  [the business workflow](docs/architecture/business_workflow.md).
- **Deterministic engines:** material and manufacturing validation, inventory, weekly
  capacity planning, lead time, costing, pricing, multi-part quotation and approval
  evaluation.
  - Every result records the rule versions and fingerprints it used.
  - Each evaluation reads exactly one inventory snapshot and one capacity snapshot.
- **Backend:** FastAPI with strict Pydantic v2 contracts, structured validation errors
  that never echo submitted values, and opaque 503 responses that never expose
  infrastructure details.
- **Data:** PostgreSQL 18, async SQLAlchemy 2 with explicit loading, and Alembic
  migrations guarded by schema-contract checks.
- **Security:**
  - Asymmetric JWT verification with key rotation and optional token revocation.
  - Roles and permissions read from the database on every request; token claims never
    grant access.
  - Customer filtering in SQL; another customer's resource returns 404.
  - Read-only database transactions on read routes.
- **Attachments:** PDF and CSV upload with authorization before the body is read, size
  limits, PDF parsing in a resource-limited child process, files stored without
  overwriting, idempotent retries, and an operator reconciliation tool.
- **Tool integration (planned):** an MCP server exposing the deterministic services to
  the agents.

## What exists today and what is planned

**Implemented and tested**

- PostgreSQL schema, migrations, seeding and an integrity audit over a synthetic dataset.
- All deterministic business engines and the approval evaluation. A human-release
  requirement is enforced in code.
- A secured FastAPI backend:
  - customer profile, RFQs, the material catalog and released quotations (read);
  - PDF/CSV attachment upload and listing for RFQs.
- Operator CLIs for identity bindings, operator accounts, RFQ staff assignments and
  attachment reconciliation. All are dry run by default and audited.

**Planned (not yet built)**

- The LangGraph supervisor and the specialist agents.
- The MCP tool server.
- RFQ submission and quotation write APIs, persistence of quote drafts, and the
  human-in-the-loop approval workflow.
- A review frontend and Docker packaging.

## Technology

Python 3.12 · FastAPI · Pydantic v2 · PostgreSQL 18 · SQLAlchemy 2 (async) · Alembic ·
PyJWT · pytest · Ruff · uv · GitHub Actions. LangGraph and MCP are planned.

The engineering practices:

- `Decimal` arithmetic end to end, rounded with ROUND_HALF_UP.
- Immutable result objects.
- Caller-owned database sessions.
- Business rules stored in the database and pinned by version and hash.
- Fail-closed security defaults.
- Human-in-the-loop release.
- An extensive unit, integration, security and migration-lifecycle test suite.

## Local setup

**Prerequisites:** Python 3.12, [uv](https://docs.astral.sh/uv/) and PostgreSQL 18 on
localhost.

The database safety checks accept only:

- databases named `rfq_copilot_dev` and `rfq_copilot_test`, both owned by the connecting
  role;
- a role with no SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS privileges.

```sql
CREATE ROLE rfq_copilot_dev LOGIN PASSWORD '<choose one>';
CREATE DATABASE rfq_copilot_dev  OWNER rfq_copilot_dev ENCODING 'UTF8' TEMPLATE template0;
CREATE DATABASE rfq_copilot_test OWNER rfq_copilot_dev ENCODING 'UTF8' TEMPLATE template0;
```

```bash
uv sync --locked
cp .env.example .env && chmod 600 .env
# Set DATABASE_URL, TEST_DATABASE_URL and TEST_DATABASE_NAME=rfq_copilot_test
```

**Dataset.** The synthetic dataset is private, because it contains held-out evaluation
answers. Authorized developers receive the archive separately, place it under `data/`
and extract it there. With the dataset in place, migrate and seed the development
database:

```bash
P=.venv/bin/python
D=(--source data/rfq_synthetic_data --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env)
$P -m alembic -x target=development -x env_file=.env upgrade head
$P -m rfq_copilot.seeding.cli --target development "${D[@]}" --dry-run
$P -m rfq_copilot.seeding.cli --target development "${D[@]}" --apply --confirm-database rfq_copilot_dev
$P -m rfq_copilot.integrity --target development "${D[@]}"      # read-only audit
```

Leave `rfq_copilot_test` empty. The test fixtures migrate, seed and tear it down
themselves. Seeding details: [docs/development/database_seeding.md](docs/development/database_seeding.md).

## Running the API

```bash
RFQ_API_ENVIRONMENT=development uv run uvicorn --factory rfq_copilot.api.app:create_app --host 127.0.0.1 --port 8000
```

`/health/live`, `/api/v1/system/info` and, in development only, the interactive docs at
`/docs` need no credentials.

All other routes under `/api/v1` need a bearer JWT from a configured issuer
(`RFQ_API_JWT_*` settings), plus a database customer binding and role permission.
Settings come only from `RFQ_API_*` process variables, and an unknown or invalid value
stops startup.

Routes, permissions and settings are documented in
[the backend report](docs/development/phase_3_final_backend_report.md).

## Testing

| Command | Needs | Covers |
| --- | --- | --- |
| `uv run pytest -q -p no:cacheprovider tests/test_database_config.py tests/test_hardening_public.py tests/test_api.py tests/test_api_schemas.py tests/test_api_reads.py tests/test_attachments.py tests/test_auth.py tests/test_rbac.py tests/test_phase3_hardening.py tests/test_phase3_reconcile.py tests/test_phase35_completion.py` | nothing | Public smoke suite: API, validation, JWT and RBAC (run in [CI](.github/workflows/smoke.yml)) |
| `.venv/bin/python -m pytest -q` | dataset, `.env`, PostgreSQL | Full regression: services, database lifecycle, end-to-end API security |
| `.venv/bin/ruff check .` and `.venv/bin/ruff format --check src tests alembic` | nothing | Lint and format |
| `uv lock --check` | nothing | The lockfile is current |

Without the private dataset, run only the smoke suite.

## Known limitations

- Agents, the MCP server, write APIs, quote persistence and the approval workflow are not
  built yet.
- The new security features are implemented but off by default. Each must be enabled per
  deployment: permission policy v2, token revocation, upload idempotency and RFQ-level
  staff assignments.
- Multi-customer staff access depends on a deferred migration.
- Customer-requested extra operations are refused until the intake contract supports them.
- Live data-freshness checks and read-consistency revalidation before persistence are
  still open.
- There is no login endpoint (tokens come from an external issuer), and there is no OCR
  or attachment download.

Full register: [docs/development/technical_debt.md](docs/development/technical_debt.md).

## Repository structure

- `src/rfq_copilot/`: services, models, repositories, seeding, the API (`api/`) and operator CLIs
- `alembic/`: database migrations
- `tests/`: unit, integration, security and lifecycle tests
- `docs/`: architecture, data, database and development documentation
- `data/`: private synthetic dataset (not committed)
