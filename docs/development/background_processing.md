# Continuous background RFQ processing

Customer submission → PostgreSQL queue → Supervisor worker → deterministic internal
quotation draft → human review, independent approval and authorized release → customer view.

The workflow worker and quotation drafter run as separate continuous consumers. PostgreSQL
owns queue state, leases and operation receipts. The ordinary API accepts customer input;
a separate decision API owns human approval, rejection and release.

## Prerequisites

Run commands from the repository root in a POSIX shell with Python dependencies installed.
Frontend commands also require the dependencies under `frontend/`. Follow the
[project setup overview](../../README.md#local-setup) for installation.

The local environment must already have:

- PostgreSQL 18 on loopback with the current schema contract (`20261118_01`), the private
  frozen dataset, protected snapshots, and registered business rules.
- Registered RBAC overlay v4, API permission policy v3, customer/staff identities and bindings,
  an eligible per-customer workflow service identity, and any required RFQ assignments.
- Reviewed ordinary application, workflow, drafter and human-decision database roles.
  See the [runtime](../database/k2_runtime_roles.sql),
  [workflow](../database/a1_workflow_role.sql), [drafter](../database/a4_drafter_role.sql)
  and [decision](../database/k5_decision_roles.sql) provisioning references.
- Recorded queue activation and `RFQ_API_WORKFLOW_QUEUE=enabled`. The
  [queue administration entry point](../../src/rfq_copilot/workflow_queue.py) requires an
  authorized administrator and explicit target confirmation. Enabling the environment
  setting alone does not activate the queue.
- Existing JWT trust configuration and matching signing/public keys, plus an installed
  local Ollama model selected for the worker.

For a fresh environment, review [migrations](local_development.md),
[seeding](local_development.md), [identity provisioning](../../src/rfq_copilot/operator_admin.py),
and the provisioning references before startup. Startup does not migrate, seed, issue human
tokens, create roles, or activate processing. Reuse an existing approved environment without
repeating those administrative operations.

The [environment template](../../.env.example) contains placeholders and non-secret defaults.
Replace its role/password, absolute key/storage path, and model placeholders privately.
Keep existing `.env` files and credentials private.

## Process configuration and security boundaries

| Process | Selected development URL | Other settings |
| --- | --- | --- |
| Ordinary API | `APP_DATABASE_URL` | `RFQ_API_*`: JWT verification, customer policy, queue switch, upload storage |
| Human-decision API | `DECISION_DATABASE_URL` | `RFQ_API_*`: JWT verification and authorization |
| MCP host | `APP_DATABASE_URL` | `RFQ_MCP_*`: trust, host policy, read-only business transactions |
| Workflow worker | `WORKFLOW_DATABASE_URL` | `RFQ_WORKFLOW_*`, MCP trust, and REST audience identifier |
| Quotation drafter | `DRAFTER_DATABASE_URL` | `RFQ_DRAFTER_*`; no model, MCP credential or decision URL |

All processes validate `DATABASE_HOST`, `DATABASE_PORT`, and
`DEVELOPMENT_DATABASE_NAME`. Each restricted session checks live database authority before
accessing business state. The selected URL never falls back to owner credentials.

MCP and the worker require `RFQ_MCP_RESOURCE_SERVER_URL`, `RFQ_MCP_JWT_ISSUER`,
`RFQ_MCP_JWT_AUDIENCE`, `RFQ_MCP_JWT_ALGORITHM`, and
`RFQ_MCP_JWT_PUBLIC_KEY_FILE`. The MCP host additionally needs
`RFQ_MCP_HOST_PUBLIC_BASE_URL`, `RFQ_MCP_HOST_ALLOWED_HOSTS`, and its database target.
MCP uses permission policy v2 and enforced revocation; its audience must differ from
`RFQ_API_JWT_AUDIENCE`. Issuer and key values must match the configured token provider.

The worker requires `RFQ_WORKFLOW_LLM=ollama`, `RFQ_WORKFLOW_OLLAMA_MODEL`,
`RFQ_WORKFLOW_ENVIRONMENT=development`, `RFQ_WORKFLOW_MCP_BASE_URL`, and
`RFQ_WORKFLOW_MCP_SIGNING_KEY_FILE`. Its optional Ollama base URL defaults to
`http://127.0.0.1:11434`. Select an already installed model; startup does not download one.
Use the existing matching private key for the development service-token provider.

API, MCP and consumer entry points read process variables and do not discover `.env`
automatically. In each Python-service terminal, from the repository root, define the launcher:

```bash
source scripts/dev_env.sh
```

[`scripts/dev_env.sh`](../../scripts/dev_env.sh) defines `rfq_dev` without starting a service.
It parses `.env` as data, lets explicit process variables override file values, filters the
environment for the selected process, and executes the requested command. Only the selected
database URL and target metadata are passed alongside the relevant settings namespace.
Owner and unrelated role URLs are excluded. The MCP host and worker also receive the
non-secret REST audience identifier.

For the ordinary API, the helper selects development mode, permission policy v3 and the
enabled queue switch. It selects development mode for the decision API and worker, and the
development database target for MCP. It does not provision grants or queue activation.
Optional Langfuse variables are outside this launcher's allowlist.

Do not load the whole private file with `source .env` or `uvicorn --env-file`.
The ordinary API, MCP and consumers must not receive decision credentials. Browser
configuration must not contain database URLs, signing keys or bearer tokens.

## Local service startup

Use a separate foreground terminal for each service. Leave already running PostgreSQL or
Ollama instances in place. Start the intended existing PostgreSQL instance using its normal
service manager; no particular package manager or cluster directory is required.

If that instance is managed directly with `pg_ctl`, set `RFQ_PGDATA` to its existing data
directory in the PostgreSQL terminal. The following commands inspect/start that cluster;
they do not initialize a new one:

```bash
: "${RFQ_PGDATA:?Set RFQ_PGDATA to the existing PostgreSQL data directory}"
pg_ctl -D "$RFQ_PGDATA" status
# Run only if this intended instance is stopped:
RFQ_PG_LOG="${TMPDIR:-/tmp}/rfq-postgresql.log"
pg_ctl -D "$RFQ_PGDATA" -l "$RFQ_PG_LOG" start
```

Start Ollama only if the existing local service is stopped:

```bash
ollama serve
```

After sourcing `scripts/dev_env.sh` in each Python-service terminal, start these services:

```bash
# Secure MCP host: ordinary restricted application role, read-only tool execution.
rfq_dev mcp .venv/bin/python -m uvicorn --factory rfq_copilot.mcp.host:create_app_from_env --host 127.0.0.1 --port 8765 --no-proxy-headers --workers 1

# Ordinary API: customer submission, staff reads and submission for review.
rfq_dev api .venv/bin/python -m uvicorn --factory rfq_copilot.api.app:create_app --host 127.0.0.1 --port 8000

# Continuous Supervisor evaluation consumer.
rfq_dev worker .venv/bin/python -m rfq_copilot.workflow_worker --target development --continuous

# Continuous deterministic quotation-drafting consumer.
rfq_dev drafter .venv/bin/python -m rfq_copilot.workflow_drafter --target development --continuous

# Separate human-decision API, needed for approval, rejection and release.
rfq_dev decision .venv/bin/python -m uvicorn --factory rfq_copilot.api.decision_app:create_decision_app --host 127.0.0.1 --port 8001
```

Start the portal from another terminal at the repository root:

```bash
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173
```

Open `http://127.0.0.1:5173` and use short-lived development JWTs for provisioned identities.
The Vite development proxy routes ordinary `/api` requests to port 8000 and
`/api/v1/quote-decisions` requests to port 8001. The browser and workers do not receive
direct decision-database access. Human tokens belong in the local sign-in interface,
not in documentation or shell command examples.

If MCP startup reports missing trust fields, supply those settings to the MCP process.
Regenerating keys does not resolve absent environment configuration. A successful API
liveness response establishes process liveness only, not full workflow readiness.

## Consumer modes and timing

Continuous operation is the default mode and requires an explicit `--target development`
or `--target test`. The `--continuous` flag makes that choice explicit in launch instructions.

For a controlled single batch, `--once` claims at most one concurrency-sized batch and exits:

```bash
.venv/bin/python -m rfq_copilot.workflow_worker --target development --once
.venv/bin/python -m rfq_copilot.workflow_drafter --target development --once
```

These direct commands require the process-specific environment to have been supplied.
The one-batch mode defaults to development if its target is omitted. Use continuous consumers
for automatic processing of submissions arriving after startup.

| Flag | Worker variable | Drafter variable | Default / bounds |
| --- | --- | --- | --- |
| `--poll-seconds` | `RFQ_WORKFLOW_POLL_SECONDS` | `RFQ_DRAFTER_POLL_SECONDS` | 5 s; 1–60 s |
| `--error-backoff-max-seconds` | `RFQ_WORKFLOW_ERROR_BACKOFF_MAX_SECONDS` | `RFQ_DRAFTER_ERROR_BACKOFF_MAX_SECONDS` | 60 s; poll interval–300 s |
| `--shutdown-grace-seconds` | `RFQ_WORKFLOW_SHUTDOWN_GRACE_SECONDS` | `RFQ_DRAFTER_SHUTDOWN_GRACE_SECONDS` | 30 s; 0–300 s |
| — | `RFQ_WORKFLOW_CONCURRENCY` | `RFQ_DRAFTER_CONCURRENCY` | 1; 1–4 |

CLI timing flags override environment values. Invalid numbers, NaN and infinity fail startup
without printing the supplied values. The default lease is 120 s, the worker heartbeat is
30 s, and evaluation/drafting attempt budgets are bounded at three.

Test-target consumers select `TEST_WORKFLOW_DATABASE_URL` or `TEST_DRAFTER_DATABASE_URL`
and require matching `TEST_DATABASE_NAME` metadata. The development launcher intentionally
does not forward test URLs or that test metadata; use an explicitly isolated test environment
for direct `--target test` invocations.

## Polling, retries and recovery

Each polling round claims at most one job per free concurrency slot. All claims in a round
are collected before processing starts, so a quickly failing job cannot consume its attempts
in a tight loop. Transient tool/model outcomes follow the bounded evaluation retry policy;
business refusals are terminal.

Claim or unresolved settlement database errors apply capped exponential backoff: by default,
5, 10, 20, 40, 60, 60… seconds. A successful polling cycle resets the delay. Connection
recovery does not reset any record's attempt budget. An unsafe database role stops the process.

Both consumers use `FOR UPDATE SKIP LOCKED`, owner/claim-generation fences and exhausted-lease
finalization. Multiple consumers can claim different jobs. A stale process cannot overwrite
a newer owner's settlement; the worker cancels the Supervisor and discards results after
losing its fence.

Only `completed / ready_for_draft` outcomes or recoverable expired `drafting` leases are
draft candidates. Clarification, review, blocked and failed evaluations are excluded.
The drafter re-evaluates the stored RFQ with trusted rules. The fenced row lock covers
quotation creation, stable `wf-<workflow_run_id>` idempotency, quote linkage, audit and
settlement in one transaction. Unique quotation-per-RFQ and receipt constraints protect
against duplicate writes.

Each executable owns one database pool per event loop. Session scopes roll back and close;
tracked tasks drain before pool disposal. The worker closes its owned Ollama clients, and
MCP transport contexts close per call. Operational logs use fixed events, status/category
codes and numeric timing fields, excluding raw RFQ content and credentials.

## Manual verification

The following commands are operator checks with different prerequisites. Running the
application demonstration is separate from running regression suites.

From the repository root, the focused offline selection uses no external database/model/MCP
service:

```bash
RFQ_OLLAMA_SMOKE=0 .venv/bin/python -m pytest tests/test_workflow_background.py tests/test_workflow_worker.py tests/test_workflow_drafter.py tests/test_workflow_database_config.py -m 'not integration' -q
.venv/bin/python -m pytest tests/test_customer_quote_discovery.py tests/test_staff_quote_listing.py tests/test_runtime_quote_lifecycle.py -m 'not integration' -q
```

Database checks require the private dataset, an empty isolated `rfq_copilot_test`, matching
test metadata, and owner/test-runtime credentials. Fixtures apply/restore test schema,
seed/truncate records and configure reviewed test grants. Run serially without xdist,
against no shared test instance. MCP is mocked or in-memory for these selections:

```bash
RFQ_OLLAMA_SMOKE=0 .venv/bin/python -m pytest tests/test_workflow_queue_database.py tests/test_workflow_worker_database.py tests/test_workflow_drafter_database.py -q
.venv/bin/python -m pytest tests/test_workflow_identity_database.py tests/test_runtime_quote_lifecycle_database.py tests/test_customer_quote_discovery_database.py -q
```

For a development demonstration, run the read-only runtime integrity check before and
after the exercise:

```bash
.venv/bin/python -m rfq_copilot.integrity --target development --source data/rfq_synthetic_data --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env --runtime
```

1. Keep both continuous consumers running. Submit one new complete, draftable RFQ through
   the customer portal and record its identifier. Submit once.
2. Observe `event=work_claimed`, `event=workflow_settled`, and `event=draft_settled`.
   Automatic evaluation requires no manual evaluation command.
3. As authorized staff, refresh the quotation list and inspect the persisted draft.
   Confirm its RFQ association, draft status and single quotation family.
4. In the customer's RFQ history, confirm no quotation link is exposed before release;
   a direct request for the internal quotation must also remain inaccessible.
5. If demonstrating the human lifecycle, submit the exact version for review. Use an
   eligible independent approver for approval and a different authorized actor for release,
   with the required explicit context confirmation. Respect any policy refusal.
6. After release, refresh customer RFQ history and open the available quotation. Verify
   its released version and selling-price fields. If the exercise stops at drafting,
   leave the quotation internal.
7. Repeat runtime integrity verification. Expected business changes must be explained by
   valid provenance; the database fingerprint will naturally change after a new submission.

If clarification or engineering review is required, inspect the staff outcome and preserve
the refusal. Do not force readiness or alter protected source data to obtain a quotation.
Use the new exercise record; leave unrelated existing records unchanged.

## Shutdown

1. Stop new frontend input. Stop the ordinary and decision APIs to prevent further
   submissions or human mutations while consumers drain.
2. Press Ctrl-C once in the worker and drafter terminals. SIGTERM is also supported.
   Inspect the intended process IDs before sending any signal:

   ```bash
   pgrep -fl 'rfq_copilot.workflow_(worker|drafter)'
   # Replace the tokens with only the confirmed worker/drafter process IDs:
   kill -TERM WORKER_PID DRAFTER_PID
   ```

3. Wait for both consumers to exit with `event=consumer_stopped`. They stop claiming work,
   interrupt polling/backoff, allow the configured grace period, then cancel unfinished
   work and await transaction/custody/heartbeat cleanup.
4. Stop the frontend, MCP host, and any foreground Ollama process started for this session.
   Leave shared or previously running dependencies under their existing service management.
5. Only if you manually started the intended PostgreSQL instance, stop it after consumer
   cleanup. For a cluster managed with `pg_ctl` in the same terminal:

   ```bash
   : "${RFQ_PGDATA:?Set RFQ_PGDATA to the existing PostgreSQL data directory}"
   pg_ctl -D "$RFQ_PGDATA" stop -m fast
   ```

Worker cancellation releases its fenced claim after cleanup and refunds the attempt;
drafter cancellation rolls back before release. If PostgreSQL is unreachable, release
can fail and the lease remains recoverable on restart. Hard crashes and uncertain commits
likewise rely on lease fencing and idempotency. Exhausted attempts are finalized rather
than retried indefinitely.

The grace period bounds useful in-flight work. Awaited rollback and transport cleanup can
extend total shutdown time; it is not a hard operating-system kill deadline.

## Deployment limitations

Foreground services need open terminals and manual restart after reboot. Production process
supervision, controlled secret injection, TLS, log management, monitoring and operational
acceptance require a separately designed deployment.

The worker's service-token provider is for local development. Queue recovery re-evaluates
interrupted attempts after lease expiry; process-local Supervisor evidence does not provide
durable continuation. Model and read-only tool execution may repeat after a crash, while
fencing and deterministic idempotent writes protect authoritative persisted results.
