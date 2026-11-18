"""K2 (Day 54A) restricted runtime role against the guarded PostgreSQL test database.

Needs the manually provisioned ``rfq_copilot_app_test`` role (docs/database/k2_runtime_roles.sql,
steps 1-2) and ``TEST_APP_DATABASE_URL``; a missing credential is a failure, never a skip.
The guarded test upgrade recreates the tables, which drops table grants, so the fixture
re-applies exactly the artifact's marked grant block as the owner. The committed ``world``
is truncated by the owner in teardown. The development database is never opened.
"""

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_quote_drafts_concurrency import guarded
from test_quote_drafts_concurrency import world as world
from test_quote_drafts_database import RFQ_ID, args
from test_quote_versions_database import rargs

import rfq_copilot.rfq_submission as rfq_submission
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.schemas.rfq import RfqSubmissionRequest
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import (
    Database,
    PrivilegedRuntimeRole,
    require_restricted_runtime,
    runtime_role_violations,
)
from rfq_copilot.models import (
    RFQ,
    AuditEvent,
    Customer,
    IdempotencyRecord,
    RFQLineItem,
    User,
    UserCustomerBinding,
)
from rfq_copilot.quote_drafts import RECEIPT_KEY, create_quote_draft, violated_unique
from rfq_copilot.quote_versions import revise_quote
from rfq_copilot.seeding.policy import register_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "docs" / "database" / "k2_runtime_roles.sql"
DENIED, REFUSED = "42501", "23000"  # insufficient_privilege; K2 integrity_constraint_violation


def grant_block(role: str) -> list[str]:
    """The artifact's marked table grants, with its ``:"app"`` placeholder bound to ``role``."""
    body = ARTIFACT.read_text().split("-- BEGIN K2 TABLE GRANTS\n")[1]
    body = body.split("-- END K2 TABLE GRANTS")[0]
    return [s.strip().replace(':"app"', role) for s in body.split(";") if s.strip()]


@pytest_asyncio.fixture(loop_scope="module")
async def runtime(world, seed_database):
    config = DatabaseConfig.from_env("test", env_file=ROOT / ".env", runtime=True)
    quote = seed_database.engine.dialect.identifier_preparer.quote
    async with seed_database.session() as s, s.begin():
        await guarded(s)
        for statement in grant_block(quote(config.url.username)):
            await s.execute(text(statement))
    async with Database(config) as database:
        yield database


async def test_only_the_runtime_role_passes_the_restricted_check(runtime, seed_database):
    async with runtime.session() as s:
        await guarded(s)
        assert await runtime_role_violations(s) == ()
        await require_restricted_runtime(s)
    async with seed_database.session() as s:
        assert {"database_owner", "table_owner"} <= set(await runtime_role_violations(s))
        with pytest.raises(PrivilegedRuntimeRole):
            await require_restricted_runtime(s)


async def test_the_runtime_role_drafts_and_revises_under_the_triggers(runtime, world):
    async with runtime.session() as s, s.begin():
        await guarded(s)
        draft = await create_quote_draft(s, world, RFQ_ID, **args())
    async with runtime.session() as s, s.begin():
        revision = await revise_quote(s, world, draft.quote_id, **rargs())
    assert (draft.version, revision.version) == (1, 2)


FORGED_APPROVED_QUOTE = (  # an approved quotation inserted directly, never approved by anyone
    "INSERT INTO business.quotes (quote_id, rfq_id, current_version, status, created_by, "
    "created_at, approved_version, approver_id, approval_timestamp) "
    "SELECT 'QUOTE-K2-FORGED', 'TEST-DAY51-RFQ-FORGED', current_version, status, created_by, "
    "created_at, approved_version, approver_id, approval_timestamp "
    "FROM business.quotes WHERE status = 'approved' LIMIT 1"
)
FORGED_VALID_VERSION = (  # a new version claiming a valid approval
    "INSERT INTO business.quote_versions (quote_version_id, quote_id, version, created_by, "
    "created_at, is_current, approval_valid, total_usd, pricing_rule_version, "
    "snapshot_completeness) "
    "SELECT quote_version_id || '-F', quote_id, version + 100, created_by, created_at, "
    "false, true, total_usd, pricing_rule_version, snapshot_completeness "
    "FROM business.quote_versions WHERE total_usd IS NOT NULL LIMIT 1"
)
LINE_COPY = (
    "INSERT INTO business.quote_line_items (quote_line_id, quote_id, quote_version, line_number, "
    "part_id, part_json, cost_breakdown_json, quantity, line_total_usd) "
    "SELECT quote_line_id || '-R', quote_id, quote_version, 99, part_id, part_json, "
    "cost_breakdown_json, quantity, line_total_usd FROM business.quote_line_items LIMIT 1"
)


# K3 (Day 54B): runtime inserts cannot invent a creation time.
BACKDATED_VERSION = (  # a partial version claiming yesterday's creation time
    "INSERT INTO business.quote_versions (quote_version_id, quote_id, version, created_by, "
    "created_at, is_current, approval_valid, total_usd, pricing_rule_version, "
    "snapshot_completeness) "
    "SELECT quote_version_id || '-B', quote_id, version + 200, created_by, "
    "created_at - interval '1 day', false, false, total_usd, pricing_rule_version, "
    "'priced_partial' FROM business.quote_versions WHERE quote_id LIKE 'QUOTE-%' LIMIT 1"
)
BACKDATED_RECORD = (  # a calculation record claiming yesterday's creation time
    "INSERT INTO business.pricing_calculations (calculation_record_id, calculation_id, rfq_id, "
    "created_by, created_at, requirement_sha256, parts, lines, quote_total_usd, "
    "pricing_rule_version, manufacturing_rule_version, source_snapshot_versions, "
    "content_sha256) "
    "SELECT calculation_record_id || '-B', calculation_id, rfq_id, created_by, "
    "created_at - interval '1 day', requirement_sha256, parts, lines, quote_total_usd, "
    "pricing_rule_version, manufacturing_rule_version, source_snapshot_versions, "
    "content_sha256 FROM business.pricing_calculations LIMIT 1"
)
RULE_ROW = (
    "INSERT INTO business.business_rule_versions (rule_version, rule_kind, sha256, document, "
    "source_path) SELECT 'workflow-v9.9', rule_kind, repeat('e', 64), document, 'test://k3' "
    "FROM business.business_rule_versions WHERE rule_version = 'workflow-v1.2'"
)


@pytest.mark.parametrize(
    "statement,sqlstate",
    [(BACKDATED_VERSION, REFUSED),
     (BACKDATED_RECORD, REFUSED),
     ("UPDATE business.pricing_calculations SET quote_total_usd = 0", DENIED),
     ("DELETE FROM business.pricing_calculations", DENIED),
     (RULE_ROW, DENIED),  # INSERT is not in RUNTIME_INSERTABLE: never granted
     ("UPDATE business.business_rule_versions SET source_path = 'x'", DENIED),
     ("DELETE FROM business.business_rule_versions", DENIED),
     ("UPDATE business.rfqs SET status = 'closed'", DENIED),  # INSERT only (Milestone 2 Step 2)
     ("DELETE FROM business.rfq_line_items", DENIED),
     ("INSERT INTO business.users (user_id, display_name, role, email, active, assigned_region, authentication_subject) VALUES ('U-X', 'X', 'customer', 'x@x.invalid', true, 'NA', 'auth|x')", DENIED),
     ("ALTER TABLE business.quote_versions DISABLE TRIGGER k3_quote_versions_evidence", DENIED),
     ("SET CONSTRAINTS business.k3_quote_version_lines_complete DEFERRED", None)],
)  # fmt: skip
async def test_k3_runtime_writes_are_bounded(runtime, world, statement, sqlstate):
    async with runtime.session() as s, s.begin():
        await create_quote_draft(s, world, RFQ_ID, **args())  # a complete version + record
    async with runtime.session() as s:  # rolled back on exit
        await guarded(s)
        if sqlstate is None:  # deferring is allowed: the check still runs at COMMIT
            await s.execute(text(statement))
            return
        with pytest.raises(DBAPIError) as caught:
            await s.execute(text(statement))
        assert caught.value.orig.sqlstate == sqlstate


async def test_an_extra_insert_grant_is_a_runtime_violation(runtime, seed_database):
    role = seed_database.engine.dialect.identifier_preparer.quote(runtime.config.url.username)
    async with seed_database.session() as s, s.begin():
        await guarded(s)
        await s.execute(text(f"GRANT INSERT ON business.customers TO {role}"))
    try:
        with pytest.raises(PrivilegedRuntimeRole, match="insert_privileges"):
            async with runtime.session():
                pytest.fail("An overprivileged runtime session reached the caller")
    finally:
        async with seed_database.session() as s, s.begin():
            await guarded(s)
            await s.execute(text(f"REVOKE INSERT ON business.customers FROM {role}"))
    async with runtime.session() as s:
        assert await runtime_role_violations(s) == ()


@pytest.mark.parametrize(
    "statement,sqlstate",
    [("UPDATE business.quote_versions SET total_usd = 0", DENIED),
     ("UPDATE business.quote_versions SET is_current = true", REFUSED),
     ("DELETE FROM business.quote_versions", DENIED),
     ("UPDATE business.quote_line_items SET quantity = 1", DENIED),
     ("DELETE FROM business.quote_line_items", DENIED),
     (LINE_COPY, REFUSED),
     (FORGED_APPROVED_QUOTE, REFUSED),
     (FORGED_VALID_VERSION, REFUSED),
     ("UPDATE business.audit_events SET outcome = 'error'", DENIED),
     ("DELETE FROM business.audit_events", DENIED),
     ("UPDATE business.idempotency_records SET correlation_id = 'x'", DENIED),
     ("DELETE FROM business.idempotency_records", DENIED),
     ("UPDATE business.quotes SET status = 'approved'", REFUSED),
     ("UPDATE business.quotes SET approver_id = NULL", DENIED),  # K5: a decision column, never granted to the app role
     ("UPDATE business.quotes SET rejection_reason = 'x'", REFUSED),  # granted (K5 revision); k5_quotes_guard refuses
     ("UPDATE business.quotes SET created_by = created_by", DENIED),  # never granted
     ("DELETE FROM business.quotes", DENIED),
     ("TRUNCATE business.audit_events", DENIED),
     ("ALTER TABLE business.audit_events DISABLE TRIGGER k2_audit_events_immutable", DENIED),
     ("ALTER TABLE business.quote_versions DISABLE TRIGGER ALL", DENIED),
     ("DROP TRIGGER k2_rfqs_immutable ON business.rfqs", DENIED),
     ("DROP FUNCTION business.k2_refuse_change() CASCADE", DENIED),
     ("CREATE TABLE business.k2_probe (id integer)", DENIED),
     ("CREATE FUNCTION business.k2_probe() RETURNS integer LANGUAGE sql AS 'SELECT 1'", DENIED),
     ("SELECT business.k2_refuse_change() IS NULL", DENIED)],  # bare call: asyncpg has no codec for the trigger pseudo-type, so it never executed
)  # fmt: skip
async def test_the_runtime_role_cannot_rewrite_or_unprotect_history(runtime, world, statement, sqlstate):  # fmt: skip
    async with runtime.session() as s, s.begin():
        await create_quote_draft(s, world, RFQ_ID, **args())  # committed history to attack
    async with runtime.session() as s:  # rolled back on exit
        await guarded(s)
        with pytest.raises(DBAPIError) as caught:
            await s.execute(text(statement))
        assert caught.value.orig.sqlstate == sqlstate


async def test_the_runtime_role_cannot_become_the_owner(runtime, seed_database):
    owner = seed_database.engine.dialect.identifier_preparer.quote(seed_database.config.url.username)  # fmt: skip
    async with runtime.session() as s:
        await guarded(s)
        with pytest.raises(DBAPIError) as caught:
            await s.execute(text(f"SET ROLE {owner}"))
        assert caught.value.orig.sqlstate == DENIED


# --- Milestone 2 Step 2: customer RFQ creation under the restricted runtime role ----------


async def runtime_customer(seed_database, world, user_id="U-CUST-RT"):
    """A committed customer account bound to the world's customer (truncated in teardown)."""
    async with seed_database.session() as s, s.begin():
        await guarded(s)
        await register_policy(s, "rbac-policy-v3")
        s.add(User(user_id=user_id, display_name="Buyer", role="customer", email=f"{user_id}@customer.invalid",
                   active=True, assigned_region="NA", authentication_subject=f"auth|{user_id}"))  # fmt: skip
        await s.flush()
        await s.execute(update(Customer).where(Customer.customer_id == world.customer_id).values(account_status="active"))  # fmt: skip
        s.add(UserCustomerBinding(user_id=user_id, customer_id=world.customer_id))
    return Principal(world.customer_id, frozenset(), user_id=user_id)


async def test_the_receipt_constraint_name_is_the_stored_one(runtime, seed_database):
    async with seed_database.session() as s:
        await guarded(s)
        names = (await s.scalars(text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'business.idempotency_records'::regclass AND contype = 'u'"
        ))).all()  # fmt: skip
    assert names == [RECEIPT_KEY]  # replay paths match 23505 errors by this name


async def test_concurrent_first_attempts_create_exactly_one_rfq(runtime, seed_database, world, monkeypatch):  # fmt: skip
    principal = await runtime_customer(seed_database, world)
    barrier = asyncio.Barrier(2)
    real = rfq_submission._check_references

    async def held(*args):  # both requests pass every pre-check before either writes
        await barrier.wait()
        return await real(*args)

    monkeypatch.setattr(rfq_submission, "_check_references", held)
    request = RfqSubmissionRequest.model_validate({"parts": [{"part_id": "P-RACE", "quantity": 5}]})

    async def attempt():
        async with runtime.session() as s, s.begin():
            await guarded(s)
            try:
                return await rfq_submission.create_customer_rfq(s, principal, request, "race-key")
            except IntegrityError as error:  # Database.session would hide it: code + name only
                pytest.fail(f"unhandled {getattr(error.orig, 'sqlstate', None)} on {violated_unique(error)}")  # fmt: skip

    (a, a_replayed), (b, b_replayed) = await asyncio.wait_for(asyncio.gather(attempt(), attempt()), 30)  # fmt: skip
    assert a == b and sorted([a_replayed, b_replayed]) == [False, True]
    async with runtime.session() as s:
        made = (
            await s.scalars(select(RFQ.rfq_id).where(RFQ.created_by == principal.user_id))
        ).all()
        assert made == [a.rfq_id]
        assert await s.scalar(select(func.count()).select_from(RFQLineItem).where(RFQLineItem.rfq_id == a.rfq_id)) == 1  # fmt: skip
        assert await s.scalar(select(func.count()).select_from(IdempotencyRecord).where(IdempotencyRecord.operation == "create_rfq")) == 1  # fmt: skip
        assert await s.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.rfq_id == a.rfq_id)) == 1  # fmt: skip
        assert await runtime_role_violations(s) == ()  # the grant stays within the allowlist
