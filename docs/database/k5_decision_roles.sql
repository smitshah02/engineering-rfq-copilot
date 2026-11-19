-- K5 (human decision) human-decision database authority. REVIEW ONLY: never executed by Claude, by
-- migrations or by application code. No password appears here; set it interactively.
--
-- Roles (cluster level, created once by a superuser):
--   rfq_quote_decision         NOLOGIN group. The k5_* triggers record approve, reject,
--                              release, approval_valid and request resolution only for its
--                              members (pg_has_role ... 'MEMBER'). Owns nothing.
--   rfq_copilot_decision       LOGIN, member of the group  -> DECISION_DATABASE_URL
--   rfq_copilot_decision_test  LOGIN, member of the group  -> TEST_DECISION_DATABASE_URL
--
-- Only the human decision process (rfq_copilot.api.decision_app) holds a decision login.
-- Never grant rfq_quote_decision to rfq_copilot_app / rfq_copilot_app_test, to an agent or
-- MCP credential, or to the owner. rfq_copilot.database.require_decision_runtime verifies
-- the live session (member, decision privileges only, nothing else); the app role's check
-- now fails if it is a member.
--
-- ---------------------------------------------------------------------------------------
-- STEP 1 - as a superuser, once per cluster.
-- ---------------------------------------------------------------------------------------
--   CREATE ROLE rfq_quote_decision        NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
--                                         NOREPLICATION NOBYPASSRLS NOINHERIT;
--   CREATE ROLE rfq_copilot_decision      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
--                                         NOREPLICATION NOBYPASSRLS INHERIT;
--   CREATE ROLE rfq_copilot_decision_test LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
--                                         NOREPLICATION NOBYPASSRLS INHERIT;
--   GRANT rfq_quote_decision TO rfq_copilot_decision;
--   GRANT rfq_quote_decision TO rfq_copilot_decision_test;
--   \password rfq_copilot_decision
--   \password rfq_copilot_decision_test
-- INHERIT is required: the logins use the group's table privileges without SET ROLE.
--
-- ---------------------------------------------------------------------------------------
-- STEP 2 - per database, as a superuser or the database owner, AFTER the K5 upgrade and
-- after re-running docs/database/k2_runtime_roles.sql STEP 2 (which removes the decision
-- grants from the app role).
-- ---------------------------------------------------------------------------------------
--   development:  psql ... -d rfq_copilot_dev  -v decision=rfq_copilot_decision      -v db=rfq_copilot_dev  -f docs/database/k5_decision_roles.sql
--   test:         psql ... -d rfq_copilot_test -v decision=rfq_copilot_decision_test -v db=rfq_copilot_test -f docs/database/k5_decision_roles.sql
--
GRANT CONNECT ON DATABASE :"db" TO :"decision";

-- Table level. Tables recreated by a downgrade/upgrade lose these grants, so re-run this
-- block after any such cycle. tests/test_quote_approval_database.py re-applies exactly the
-- statements between the two markers (":"decision"" replaced by the quoted test login).
-- BEGIN K5 DECISION GRANTS
REVOKE ALL ON ALL TABLES IN SCHEMA business, orchestration FROM :"decision";
REVOKE ALL ON ALL TABLES IN SCHEMA business, orchestration FROM rfq_quote_decision;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA business, orchestration FROM rfq_quote_decision;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA business, orchestration FROM rfq_quote_decision;
GRANT USAGE ON SCHEMA business, orchestration TO rfq_quote_decision;
GRANT SELECT ON ALL TABLES IN SCHEMA business, orchestration TO rfq_quote_decision;
GRANT INSERT ON business.approval_history, business.audit_events, business.idempotency_records TO rfq_quote_decision;
GRANT UPDATE (status, row_version, approved_version, approver_id, approval_timestamp, rejection_reason, released_by, released_at) ON business.quotes TO rfq_quote_decision;
GRANT UPDATE (approval_valid) ON business.quote_versions TO rfq_quote_decision;
GRANT UPDATE (status, resolved_by, resolved_at) ON business.approval_requests TO rfq_quote_decision;
-- END K5 DECISION GRANTS
--
-- Not granted, deliberately: INSERT on quotes, versions, lines, requests or calculations
-- (drafting, revision and submission stay with the app role); DELETE, TRUNCATE, TRIGGER,
-- REFERENCES anywhere; CREATE; EXECUTE; sequences. Row locks (FOR NO KEY UPDATE on quotes)
-- are covered by the column UPDATE grant.
--
-- ---------------------------------------------------------------------------------------
-- STEP 3 - verify (expect the stated results).
-- ---------------------------------------------------------------------------------------
--   As the decision login:
--     SELECT pg_has_role(current_user, 'rfq_quote_decision', 'MEMBER');        -- true
--     UPDATE business.quote_versions SET total_usd = 0;                         -- ERROR: permission denied
--     INSERT INTO business.quotes DEFAULT VALUES;                               -- ERROR: permission denied
--   As the app login:
--     SELECT pg_has_role(current_user, 'rfq_quote_decision', 'MEMBER');        -- false
--     SET ROLE rfq_quote_decision;                                              -- ERROR: permission denied
--     UPDATE business.quotes SET approver_id = NULL;                            -- ERROR: permission denied
--
-- Rollback: REVOKE the block's grants from rfq_quote_decision, REVOKE CONNECT from the
-- login, REVOKE rfq_quote_decision FROM the logins, then DROP ROLE. Without the group every
-- decision is refused by the k5_* triggers (fail closed); drafting and submission continue.
