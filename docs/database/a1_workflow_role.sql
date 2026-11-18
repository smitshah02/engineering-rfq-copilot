-- Checkpoints A1/A3: the dedicated workflow worker role. REVIEW ONLY: never executed by
-- Claude, by migrations or by application code. No password appears here.
--
--   rfq_copilot_workflow       (development) -> WORKFLOW_DATABASE_URL
--   rfq_copilot_workflow_test  (test only)   -> TEST_WORKFLOW_DATABASE_URL
--
-- Same shape as the K2 runtime role (docs/database/k2_runtime_roles.sql): owns nothing,
-- member of nothing (and never of rfq_quote_decision), no CREATE, no DELETE/TRUNCATE/
-- TRIGGER/REFERENCES, no EXECUTE, no sequences. It may read the business and orchestration
-- schemas, write audit events and update only the queue columns of workflow runs listed in
-- rfq_copilot.database.WORKFLOW_UPDATABLE. It cannot insert RFQs or runs, and cannot write
-- any quotation, approval or rule table. rfq_copilot.database.require_workflow_runtime
-- verifies all of that on every worker session.
--
-- STEP 1 - as a superuser, once per cluster:
--   CREATE ROLE rfq_copilot_workflow      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
--                                         NOREPLICATION NOBYPASSRLS NOINHERIT;
--   CREATE ROLE rfq_copilot_workflow_test LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
--                                         NOREPLICATION NOBYPASSRLS NOINHERIT;
--   \password rfq_copilot_workflow
--   \password rfq_copilot_workflow_test
--
-- STEP 2 - per database, as the database owner, after `alembic upgrade head` (20261117_02):
--   psql ... -d rfq_copilot_dev  -v worker=rfq_copilot_workflow      -v db=rfq_copilot_dev  -f docs/database/a1_workflow_role.sql
--   psql ... -d rfq_copilot_test -v worker=rfq_copilot_workflow_test -v db=rfq_copilot_test -f docs/database/a1_workflow_role.sql
GRANT CONNECT ON DATABASE :"db" TO :"worker";

-- Tables recreated by a downgrade/upgrade lose these grants: re-run after any such cycle.
-- tests/test_workflow_worker_database.py applies exactly the statements between the markers.
-- No semicolon may appear inside this block's comments: the test fixture splits on it.
-- BEGIN A1 WORKFLOW GRANTS
REVOKE ALL ON ALL TABLES IN SCHEMA business, orchestration FROM :"worker";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA business, orchestration FROM :"worker";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA business, orchestration FROM :"worker";
GRANT USAGE ON SCHEMA business, orchestration TO :"worker";
GRANT SELECT ON ALL TABLES IN SCHEMA business, orchestration TO :"worker";
GRANT INSERT ON business.audit_events TO :"worker";
GRANT UPDATE (status, lease_owner, lease_expires_at, attempt_count, claim_generation, disposition, last_error, external_workflow_id, updated_at) ON orchestration.workflow_runs TO :"worker";
-- END A1 WORKFLOW GRANTS
--
-- SELECT ... FOR UPDATE SKIP LOCKED on workflow_runs is covered by the column UPDATE grant.
--
-- STEP 3 - verify, connected as the worker role:
--   UPDATE orchestration.workflow_runs SET rfq_id = rfq_id;          -- ERROR: permission denied
--   INSERT INTO business.rfqs DEFAULT VALUES;                         -- ERROR: permission denied
--   UPDATE business.quotes SET status = status;                       -- ERROR: permission denied
--
-- Rollback: re-run the REVOKE lines of the block, REVOKE CONNECT, then DROP ROLE.
