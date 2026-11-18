-- Checkpoint A4: the dedicated quotation-drafter role. REVIEW ONLY: never executed by Claude,
-- by migrations or by application code. No password appears here.
--
--   rfq_copilot_drafter       (development) -> DRAFTER_DATABASE_URL
--   rfq_copilot_drafter_test  (test only)   -> TEST_DRAFTER_DATABASE_URL
--
-- Same shape as the K2 runtime and A1 worker roles: owns nothing, member of nothing (never of
-- rfq_quote_decision), no CREATE, no DELETE/TRUNCATE/TRIGGER/REFERENCES, no EXECUTE, no
-- sequences. It may read the business and orchestration schemas, INSERT exactly the rows of
-- one new draft (rfq_copilot.database.DRAFTER_INSERTABLE: quote, version 1, its lines, the K3
-- calculation record, the idempotency receipt, audit events), and UPDATE only the drafting
-- columns of workflow runs (DRAFTER_UPDATABLE). It cannot UPDATE any quote or version (no
-- revision, submission, approval, rejection or release), cannot write approval requests or
-- history, RFQs, users or bindings, and cannot insert runs. K2-K5 triggers bind it like any
-- non-owner. rfq_copilot.database.require_drafter_runtime verifies all of that on every
-- drafter session.
--
-- STEP 1 - as a superuser, once per cluster:
--   CREATE ROLE rfq_copilot_drafter      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
--                                        NOREPLICATION NOBYPASSRLS NOINHERIT;
--   CREATE ROLE rfq_copilot_drafter_test LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
--                                        NOREPLICATION NOBYPASSRLS NOINHERIT;
--   \password rfq_copilot_drafter
--   \password rfq_copilot_drafter_test
--
-- STEP 2 - per database, as the database owner, after `alembic upgrade head` (20261118_01):
--   psql ... -d rfq_copilot_dev  -v drafter=rfq_copilot_drafter      -v db=rfq_copilot_dev  -f docs/database/a4_drafter_role.sql
--   psql ... -d rfq_copilot_test -v drafter=rfq_copilot_drafter_test -v db=rfq_copilot_test -f docs/database/a4_drafter_role.sql
GRANT CONNECT ON DATABASE :"db" TO :"drafter";

-- Tables recreated by a downgrade/upgrade lose these grants: re-run after any such cycle.
-- tests/test_workflow_drafter_database.py applies exactly the statements between the markers.
-- No semicolon may appear inside this block's comments: the test fixture splits on it.
-- BEGIN A4 DRAFTER GRANTS
REVOKE ALL ON ALL TABLES IN SCHEMA business, orchestration FROM :"drafter";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA business, orchestration FROM :"drafter";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA business, orchestration FROM :"drafter";
GRANT USAGE ON SCHEMA business, orchestration TO :"drafter";
GRANT SELECT ON ALL TABLES IN SCHEMA business, orchestration TO :"drafter";
GRANT INSERT ON business.quotes, business.quote_versions, business.quote_line_items, business.pricing_calculations, business.idempotency_records, business.audit_events TO :"drafter";
GRANT UPDATE (status, lease_owner, lease_expires_at, claim_generation, draft_attempt_count, disposition, last_error, quote_id, updated_at) ON orchestration.workflow_runs TO :"drafter";
-- END A4 DRAFTER GRANTS
--
-- A first draft needs no UPDATE of quotes: quotes.current_version is set at INSERT through the
-- one deferrable foreign key, and pricing_calculations uses INSERT ... ON CONFLICT DO NOTHING.
-- SELECT ... FOR UPDATE SKIP LOCKED and the fenced UPDATE on workflow_runs are covered by the
-- column UPDATE grant. Foreign-key checks (quote_id -> quotes) run as the table owner.
--
-- STEP 3 - verify, connected as the drafter role:
--   UPDATE business.quotes SET status = status;                       -- ERROR: permission denied
--   UPDATE business.quote_versions SET is_current = is_current;       -- ERROR: permission denied
--   INSERT INTO business.approval_history DEFAULT VALUES;              -- ERROR: permission denied
--   UPDATE orchestration.workflow_runs SET rfq_id = rfq_id;           -- ERROR: permission denied
--
-- Rollback: re-run the REVOKE lines of the block, REVOKE CONNECT, then DROP ROLE.
