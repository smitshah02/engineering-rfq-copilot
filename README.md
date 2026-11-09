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
| 3+: Tool server (MCP), agents (LangGraph), API (FastAPI), persistence, review UI | 16–73 | **Not started** |

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

### Planned, not implemented

None of the following exists yet:

- the FastAPI endpoints
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
| `uv run pytest -q -p no:cacheprovider tests/test_database_config.py tests/test_hardening_public.py` | nothing | **Smoke only.** Configuration safety, canonical fingerprints, the planning-date policy and the Intake boundary. CI runs this ([.github/workflows/smoke.yml](.github/workflows/smoke.yml)). |
| `.venv/bin/python -m pytest -q` | dataset, `.env`, PostgreSQL | **Full regression.** Reference and ground-truth parity, the database lifecycle and every service (1111 tests at Phase 2.5). |
| `.venv/bin/ruff check .` and `.venv/bin/ruff format --check src tests alembic` | nothing | Lint and format |
| `uv lock --check` | nothing | The lockfile is current |

Most test modules read the frozen dataset when they are imported. Database tests fail,
rather than skip, when credentials are missing. The smoke suite is not end-to-end
verification.

Without the private dataset, run only the smoke suite. The full suite needs the dataset,
`.env` and both databases.

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

Full list: [docs/development/technical_debt.md](docs/development/technical_debt.md).

## Repository structure

- `src/rfq_copilot/`: models, repositories, seeding, integrity checks and services
- `alembic/`: migrations (`20261108_01` initial schema, `20261109_01` capacity key per snapshot)
- `tests/`: unit, integration and hardening tests
- `docs/`: architecture, data, database, development logs and reports
- `data/`: the private frozen dataset (ignored by Git)
