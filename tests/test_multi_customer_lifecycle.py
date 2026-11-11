"""Fresh-database lifecycle under the NEXT contract (deferred 20261111_05; guarded test DB).

Upgrade an empty guarded test database through 20261111_05 (alembic/proposed/deferred),
seed dataset v1.1.0, apply the pinned overlay, verify with the real seeding verifier and
integrity audit, exercise several active bindings with explicit context selection, then
restore with the guarded downgrade. NEXT is switched on only for this module; the
active CURRENT contract and the development database are untouched.
"""

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text, update
from test_auth import bearer
from test_auth import trust as trust
from test_database_migrations import alembic_config, user_objects
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_plan as seed_plan
from test_day20_auth_database import JoinedDatabase, app_with, call

from alembic import command
from rfq_copilot import identity_admin, schema_contract
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database
from rfq_copilot.integrity import audit_integrity, verify_definitions
from rfq_copilot.migration_safety import (
    MigrationSettings,
    cleanup_test_tracker,
    owned_shape,
    shape_digest,
    verify_identity,
)
from rfq_copilot.models import Customer, User, UserCustomerBinding
from rfq_copilot.seeding.loader import execute_seed
from rfq_copilot.seeding.mapping import TABLES
from rfq_copilot.seeding.policy import register_policy
from rfq_copilot.seeding.safety import verify_schema

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
ROOT = Path(__file__).resolve().parents[1]


def next_config():
    config = alembic_config(allow=True)
    config.set_main_option(
        "version_locations", os.pathsep.join(str(p) for p in schema_contract.NEXT.version_locations)
    )
    return config


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def next_database():
    config = DatabaseConfig.from_env("test", env_file=ROOT / ".env")
    settings = MigrationSettings(config, allow_test_downgrade=True)
    previous, schema_contract._ACTIVE = schema_contract._ACTIVE, schema_contract.NEXT
    try:
        async with Database(config) as database:
            async with database.engine.connect() as lease:
                assert (await lease.run_sync(verify_identity, settings))["database"] == "rfq_copilot_test"  # fmt: skip
                assert await lease.scalar(text("SELECT pg_try_advisory_lock(461441,505)"))
                await lease.rollback()
                original = await lease.run_sync(user_objects)
                assert original["relations"] == []
                await lease.rollback()
                try:
                    await asyncio.to_thread(command.upgrade, next_config(), "head")
                    yield database
                finally:
                    await lease.rollback()
                    if await lease.scalar(text("SELECT to_regclass('business.customers') IS NOT NULL")):  # fmt: skip
                        for table in TABLES.values():
                            assert await lease.scalar(select(func.count()).select_from(table)) == 0
                        await lease.rollback()
                        await asyncio.to_thread(command.downgrade, next_config(), "base")
                    await lease.run_sync(cleanup_test_tracker)
                    await lease.commit()
                    assert await lease.run_sync(user_objects) == original
                    await lease.execute(text("SELECT pg_advisory_unlock(461441,505)"))
                    await lease.rollback()
    finally:
        schema_contract._ACTIVE = previous


@pytest_asyncio.fixture(loop_scope="module")
async def session(next_database):
    async with next_database.session() as session:
        transaction = await session.begin()
        try:
            yield session
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await session.rollback()


async def seeded(session, database, plan):
    await execute_seed(session, database.config, plan, "apply", confirm_database="rfq_copilot_test")  # fmt: skip
    await register_policy(session, "rbac-policy-v2")


async def test_next_contract_pins_are_the_measured_schema(next_database):
    async with next_database.engine.connect() as connection:
        assert await connection.scalar(text("SELECT version_num FROM public.alembic_version")) == "20261111_05"  # fmt: skip
        digest = await connection.run_sync(lambda c: shape_digest(owned_shape(c)))
        assert digest == schema_contract.NEXT.shape_digest, digest
        assert digest != schema_contract.CURRENT.shape_digest
        await connection.run_sync(verify_definitions)
        assert (await connection.run_sync(verify_schema, next_database.config))["alembic_revision"] == "20261111_05"  # fmt: skip
        index = await connection.scalar(text("SELECT to_regclass('business.uq_user_customer_bindings_active_user')"))  # fmt: skip
        assert index is None


async def test_seed_overlay_and_integrity_under_next(session, next_database, seed_plan):
    await seeded(session, next_database, seed_plan)
    report = await audit_integrity(session, next_database.config, seed_plan)
    assert report["status"] == "PASS" and report["policy_overlay"] == "applied:rbac-policy-v2"
    assert report["schema_contract"] == "next-20261111_05"


async def operator(session):
    await session.execute(
        insert(User).values(
            user_id="U-OP-NEXT", display_name="Op", role="identity_operator", email="op-next@test.invalid",
            active=True, assigned_region="NA", authentication_subject="auth|op-next",
        )
    )  # fmt: skip
    return "U-OP-NEXT"


async def test_several_bindings_need_explicit_verified_context(session, next_database, seed_plan, trust):  # fmt: skip
    await seeded(session, next_database, seed_plan)
    op = await operator(session)
    user = await session.scalar(select(User).where(User.role == "sales_representative", User.active.is_(True)).order_by(User.user_id).limit(1))  # fmt: skip
    first = seed_plan.rows["rfqs"][0]["customer_id"]
    second = next(r["customer_id"] for r in seed_plan.rows["rfqs"] if r["customer_id"] != first)
    await session.execute(update(Customer).where(Customer.customer_id.in_([first, second])).values(account_status="active"))  # fmt: skip
    args = {"operator": op, "user": user.user_id, "reason": "onboarding", "execute": True}
    assert (await identity_admin.grant_binding(session, customer=first, **args)).result == "granted"
    assert (
        await identity_admin.grant_binding(session, customer=second, **args)
    ).result == "granted"  # allowed under NEXT
    assert (
        await identity_admin.grant_binding(session, customer=second, **args)
    ).result == "already_bound"
    database = JoinedDatabase(await session.connection())
    auth = bearer(trust.mint(sub=user.authentication_subject))
    single = app_with(trust, database)
    assert (
        await call(single, "GET", "/api/v1/customers/me", headers=auth)
    ).status_code == 403  # ambiguous
    selectable = app_with(trust, database, customer_context="selectable")
    assert (
        await call(selectable, "GET", "/api/v1/customers/me", headers=auth)
    ).status_code == 403  # no default
    for customer in (first, second):
        response = await call(selectable, "GET", "/api/v1/customers/me", headers={**auth, "X-RFQ-Customer-Context": customer})  # fmt: skip
        assert response.json()["customer_id"] == customer
    await identity_admin.revoke_binding(session, customer=second, **args)
    revoked = await call(selectable, "GET", "/api/v1/customers/me", headers={**auth, "X-RFQ-Customer-Context": second})  # fmt: skip
    assert revoked.status_code == 403
    single_again = await call(single, "GET", "/api/v1/customers/me", headers=auth)
    assert single_again.json()["customer_id"] == first  # one binding left: legacy behaviour


async def test_05_downgrade_refuses_while_users_hold_several_bindings(session, next_database, seed_plan):  # fmt: skip
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.util import CommandError

    await seeded(session, next_database, seed_plan)
    user = await session.scalar(select(User.user_id).where(User.active.is_(True)).order_by(User.user_id).limit(1))  # fmt: skip
    customers = sorted({r["customer_id"] for r in seed_plan.rows["rfqs"]})[:2]
    for customer in customers:
        await session.execute(
            insert(UserCustomerBinding).values(user_id=user, customer_id=customer)
        )
    (path,) = schema_contract.DEFERRED.glob("20261111_05_*.py")
    spec = importlib.util.spec_from_file_location("deferred_05", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(sync_session):
        with Operations.context(MigrationContext.configure(sync_session.connection())):
            module.downgrade()

    savepoint = await session.begin_nested()
    with pytest.raises(CommandError):
        await session.run_sync(downgrade)
    await savepoint.rollback()
