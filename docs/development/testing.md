# Verification and test isolation

Backend tests use pytest; frontend tests use Vitest and Testing Library. The public
[smoke workflow](../../.github/workflows/smoke.yml) selects tests needing neither PostgreSQL
nor the private dataset. The full local regression additionally uses protected source
fixtures and an explicitly isolated PostgreSQL database.

## Offline verification

From the repository root with existing dependencies installed:

```bash
RFQ_OLLAMA_SMOKE=0 RFQ_AGENT_LIVE_MCP=0 RFQ_LANGFUSE_SMOKE=0 .venv/bin/python -m pytest --collect-only -q -p no:cacheprovider
RFQ_OLLAMA_SMOKE=0 RFQ_AGENT_LIVE_MCP=0 RFQ_LANGFUSE_SMOKE=0 .venv/bin/python -m pytest -m 'not integration' --deselect=tests/test_backend_integration.py::test_uvicorn_serves_and_shuts_down_cleanly_without_database_credentials -q -p no:cacheprovider
.venv/bin/ruff check .
.venv/bin/ruff format --check src tests alembic
UV_OFFLINE=true uv lock --check
npm --prefix frontend run test:run
npm --prefix frontend run build
```

The explicit deselection prevents a real Uvicorn process from starting even though that
single regression is dataset-free. It remains in the suite for separately authorized
process testing. Model/export opt-ins are disabled explicitly. Unit tests use fake providers
and ASGI/in-memory transports; constructing a local model adapter does not invoke a model.
The frontend build performs TypeScript checking followed by Vite asset compilation.

## Guarded PostgreSQL tests

Inspect `tests/conftest.py`, `test_database_seeding_integration.py`,
`test_database_migrations.py`, and `migration_safety.py` before execution. Configured targets
must be `rfq_copilot_test` and `rfq_copilot_dev`, with different identities/databases.
Schema-reset fixtures construct the test target explicitly, verify live database/owner/
endpoint/version and empty original state, and acquire advisory locks before upgrades,
seeding or teardown. Downgrade requires explicit test-only permission. The seed fixture
restores the isolated database's original empty state after rollback/truncation and downgrade.

Run serially, without xdist or another concurrent database suite. Never redirect the test
URL to development. Development tests are read-only catalog/connection checks; destructive
lifecycle work is restricted to the guarded test target. Missing prerequisites are failures,
not permission to weaken guards or silently skip coverage.

The no-server integration selection excludes real gateway subprocess tests:

```bash
RFQ_OLLAMA_SMOKE=0 RFQ_AGENT_LIVE_MCP=0 RFQ_LANGFUSE_SMOKE=0 .venv/bin/python -m pytest -m integration --deselect=tests/test_mcp_live_e2e.py::test_authenticated_live_end_to_end --deselect=tests/test_mcp_client.py::test_live_uvicorn_host_smoke -q -p no:cacheprovider
```

Other specialist live-MCP modules are gated by `RFQ_AGENT_LIVE_MCP=1`; leave the gate off
unless live servers are separately authorized. `RFQ_OLLAMA_SMOKE` and `RFQ_LANGFUSE_SMOKE`
control local model and telemetry export checks. Test infrastructure can open loopback
connections to the isolated database; no paid provider is needed.

## Coverage and failure interpretation

Purpose-named database modules cover repositories, manufacturing, inventory, capacity,
lead time, costing, pricing, evaluation, API reads/uploads, authentication/RBAC and quotation
lifecycle. ORM/migration tests also parse the preserved baseline documentation contracts.
Additional tests cover structured intake/provenance, SourceGate, independent decisions,
customer visibility, runtime corruption, fencing, retries, cancellation and consumer resources.

Collection preserves test functions/assertions after renaming; shared fixture imports and CI
selectors must change atomically. Keep focused failures distinct from full-suite results.
Correct the smallest scoped cause, rerun affected tests, and report pre-existing or unresolved
failures. Do not remove assertions, broaden grants or enable live calls to force a pass.

Read-only development integrity verification is documented in
[local development](local_development.md) and [background processing](background_processing.md).
Capture its fingerprint before/after test verification when protecting an existing demo.
No test or documentation check establishes production deployment acceptance.
