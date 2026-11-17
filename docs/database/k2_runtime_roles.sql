-- K2 (Day 54A) least-privilege runtime roles. REVIEW ONLY: never executed by Claude, by
-- migrations or by application code. No password appears here; set it interactively.
--
-- Roles (cluster level, created once by a superuser):
--   migration/table owner  rfq_copilot_dev (development) and the test owner (test): already
--                          exist; DATABASE_URL / TEST_DATABASE_URL. They run migrations,
--                          seeding, integrity audits, operator CLIs and test fixtures.
--   runtime                rfq_copilot_app (development)      -> APP_DATABASE_URL
--                          rfq_copilot_app_test (test only)   -> TEST_APP_DATABASE_URL
--
-- A runtime role owns nothing, is a member of nothing (so cannot SET ROLE to the owner),
-- has no SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS, no CREATE on the database or
-- any schema, no DELETE/TRUNCATE/TRIGGER/REFERENCES on any table, UPDATE only on the
-- columns in rfq_copilot.database.RUNTIME_UPDATABLE, INSERT only on the tables in
-- rfq_copilot.database.RUNTIME_INSERTABLE (K3 added business.pricing_calculations; INSERT
-- ... ON CONFLICT DO NOTHING needs no UPDATE; K4 added approval_requests, approval_history
-- and the lifecycle columns), and no EXECUTE on the K2/K3 functions.
-- rfq_copilot.database.require_restricted_runtime verifies all of that on a live session.
--
-- ---------------------------------------------------------------------------------------
-- STEP 1 - as a superuser, once per cluster.
-- ---------------------------------------------------------------------------------------
--   CREATE ROLE rfq_copilot_app      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
--                                    NOREPLICATION NOBYPASSRLS NOINHERIT;
--   CREATE ROLE rfq_copilot_app_test LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
--                                    NOREPLICATION NOBYPASSRLS NOINHERIT;
--   \password rfq_copilot_app
--   \password rfq_copilot_app_test
-- (\password prompts; the password never reaches a file or the shell history. Put the
-- resulting URLs only in .env, never in source.)
--
-- ---------------------------------------------------------------------------------------
-- STEP 2 - per database, as a superuser or the database owner, connected to that database.
-- ---------------------------------------------------------------------------------------
--   development:  psql ... -d rfq_copilot_dev  -v app=rfq_copilot_app      -v db=rfq_copilot_dev  -f docs/database/k2_runtime_roles.sql
--   test:         psql ... -d rfq_copilot_test -v app=rfq_copilot_app_test -v db=rfq_copilot_test -f docs/database/k2_runtime_roles.sql
--
-- Database level (survives migrations). PUBLIC loses CONNECT/TEMPORARY; the owner keeps
-- every privilege as owner; only the named runtime role is added.
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO :"app";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Table level. Tables recreated by a downgrade/upgrade lose these grants, so re-run this
-- block after any such cycle. tests/test_runtime_role_database.py re-applies exactly the
-- statements between the two markers (":"app"" replaced by the quoted test role) after the
-- guarded test upgrade, so the tested privileges are this file's privileges.
-- BEGIN K2 TABLE GRANTS
REVOKE ALL ON ALL TABLES IN SCHEMA business, orchestration FROM :"app";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA business, orchestration FROM :"app";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA business, orchestration FROM :"app";
GRANT USAGE ON SCHEMA business, orchestration TO :"app";
GRANT SELECT ON ALL TABLES IN SCHEMA business, orchestration TO :"app";
GRANT INSERT ON business.rfq_attachments, business.idempotency_records, business.audit_events, business.quotes, business.quote_versions, business.quote_line_items TO :"app";
GRANT INSERT ON business.pricing_calculations TO :"app";
GRANT UPDATE (current_version, status, row_version) ON business.quotes TO :"app";
GRANT UPDATE (is_current) ON business.quote_versions TO :"app";
-- K4 (Day 54D) submission and K5 (Day 55) clearing a rejection on revision. Approve,
-- reject, release, approval_valid and request resolution belong to the decision role only
-- (docs/database/k5_decision_roles.sql), and the k5_* triggers refuse them for this role.
-- No semicolon may appear inside this block's comments: the test fixture splits on it.
GRANT INSERT ON business.approval_requests, business.approval_history TO :"app";
GRANT UPDATE (submitted_by, rejection_reason) ON business.quotes TO :"app";
-- END K2 TABLE GRANTS
--
-- Not granted, deliberately: DELETE, TRUNCATE, TRIGGER, REFERENCES anywhere; CREATE on any
-- schema; sequences (the managed schemas have none: keys are text or UUID); EXECUTE on any
-- function; any membership. No ALTER DEFAULT PRIVILEGES: a new table grants the runtime
-- role nothing until a reviewed grant names it. Row locks (FOR SHARE / FOR NO KEY UPDATE on
-- quotes) are covered by the column UPDATE grant. Foreign-key checks run as the table
-- owner, so INSERT needs no REFERENCES.
--
-- Operator CLIs (identity, assignment, operator, token revocation, seeding, policy overlay,
-- integrity) keep the owner credential; the runtime role cannot write their tables.
--
-- ---------------------------------------------------------------------------------------
-- STEP 3 - verify, connected as the runtime role (expect the stated results).
-- ---------------------------------------------------------------------------------------
--   SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
--     FROM pg_roles WHERE rolname = current_user;                    -- all false
--   SELECT pg_has_role(current_user, datdba, 'MEMBER')
--     FROM pg_database WHERE datname = current_database();           -- false
--   ALTER TABLE business.audit_events DISABLE TRIGGER k2_audit_events_immutable;
--                                                                    -- ERROR: must be owner
--   DELETE FROM business.audit_events;                               -- ERROR: permission denied
--   UPDATE business.quote_versions SET total_usd = 0;                -- ERROR: permission denied
--
-- Rollback of this file: REVOKE the grants above (or re-run the REVOKE lines of the
-- marked block), REVOKE CONNECT, then DROP ROLE once nothing depends on it.
