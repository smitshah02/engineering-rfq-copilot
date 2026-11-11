"""Fresh-database lifecycle under the active contract (guarded test database).

Upgrade an empty guarded test database to head (20261111_06; 05 stays deferred outside
alembic/versions), seed dataset v1.1.0, apply the pinned rbac-policy-v2 overlay, verify
with the real seeding verifier and integrity audit, then restore the database with the
guarded downgrade. The development database is never touched.
"""

import asyncio
import hashlib
import importlib.util
import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import CommandError
from sqlalchemy import delete, func, insert, select, text, update
from test_database_migrations import alembic_config, user_objects
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_plan as seed_plan

from alembic import command
from rfq_copilot import schema_contract
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database
from rfq_copilot.integrity import IntegrityVerificationError, audit_integrity, verify_definitions
from rfq_copilot.migration_safety import (
    MigrationSettings,
    cleanup_test_tracker,
    owned_shape,
    shape_digest,
    verify_identity,
)
from rfq_copilot.models import Permission, Role, RolePermission, User
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding import policy as policy_module
from rfq_copilot.seeding.loader import execute_seed
from rfq_copilot.seeding.mapping import TABLES
from rfq_copilot.seeding.policy import POLICIES_DIR, policy_rows, register_policy
from rfq_copilot.seeding.safety import verify_schema

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
ROOT = Path(__file__).resolve().parents[1]
OVERLAY_ROWS = 1 + 5 + 11  # identity_operator, 5 permissions, 11 grants


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def proposed_database():
    config = DatabaseConfig.from_env("test", env_file=ROOT / ".env")
    settings = MigrationSettings(config, allow_test_downgrade=True)
    assert schema_contract.active() is schema_contract.CURRENT
    if True:
        async with Database(config) as database:
            async with database.engine.connect() as lease:
                identity = await lease.run_sync(verify_identity, settings)
                assert identity["database"] == "rfq_copilot_test"
                assert await lease.scalar(text("SELECT pg_try_advisory_lock(461441,505)"))
                await lease.rollback()
                original = await lease.run_sync(user_objects)
                assert original["relations"] == [], "Existing test objects are preserved."
                await lease.rollback()
                try:
                    await asyncio.to_thread(command.upgrade, alembic_config(allow=True), "head")
                    yield database
                finally:
                    await lease.rollback()
                    if await lease.scalar(text("SELECT to_regclass('business.customers') IS NOT NULL")):  # fmt: skip
                        for table in TABLES.values():
                            assert await lease.scalar(select(func.count()).select_from(table)) == 0
                        await lease.rollback()
                        await asyncio.to_thread(command.downgrade, alembic_config(allow=True), "base")  # fmt: skip
                    await lease.run_sync(cleanup_test_tracker)
                    await lease.commit()
                    assert await lease.run_sync(user_objects) == original
                    await lease.execute(text("SELECT pg_advisory_unlock(461441,505)"))
                    await lease.rollback()


@pytest_asyncio.fixture(loop_scope="module")
async def session(proposed_database):
    async with proposed_database.session() as session:
        transaction = await session.begin()
        try:
            yield session
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await session.rollback()


async def seed(session, database, plan):
    return await execute_seed(session, database.config, plan, "apply", confirm_database="rfq_copilot_test")  # fmt: skip


def metadata_drift(connection):
    from alembic.autogenerate import compare_metadata

    from rfq_copilot.migration_safety import include_name

    context = MigrationContext.configure(
        connection,
        opts={"include_schemas": True, "include_name": include_name, "compare_type": True,
              "compare_server_default": True},
    )  # fmt: skip
    return [str(d)[:200] for d in compare_metadata(context, schema_contract.CURRENT.metadata())]


def grants(rows):
    return {(r["role"], r["permission"]) for r in rows["role_permissions"]}


# --- Contract pins ---------------------------------------------------------------------------


async def test_current_contract_pins_are_the_measured_schema(proposed_database):
    async with proposed_database.engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM public.alembic_version"))
        assert revision == schema_contract.CURRENT.revision == "20261111_06"
        digest = await connection.run_sync(lambda c: shape_digest(owned_shape(c)))
        assert digest == schema_contract.CURRENT.shape_digest, digest
        await connection.run_sync(verify_definitions)  # catalog counts p36 f71 u14 c218
        drift = await connection.run_sync(metadata_drift)
        assert drift == [], drift
        report = await connection.run_sync(verify_schema, proposed_database.config)
        assert report["alembic_revision"] == "20261111_06"
        assert digest != schema_contract.PREVIOUS.shape_digest


# --- The lifecycle ---------------------------------------------------------------------------


async def test_seed_then_overlay_then_integrity(session, proposed_database, seed_plan):
    await seed(session, proposed_database, seed_plan)
    baseline = await audit_integrity(session, proposed_database.config, seed_plan)
    assert (
        baseline["policy_overlay"] == "absent"
        and baseline["schema_contract"] == "activated-20261111_06"
    )
    assert await register_policy(session, "rbac-policy-v2") == "applied"
    assert await register_policy(session, "rbac-policy-v2") == "already present"  # repeatable
    report = await audit_integrity(session, proposed_database.config, seed_plan)
    assert report["status"] == "PASS" and report["violations"] == 0
    assert report["policy_overlay"] == "applied:rbac-policy-v2"
    assert report["records"] == baseline["records"] + OVERLAY_ROWS
    # The import verifier accepts baseline + complete overlay as the imported state.
    verified = await execute_seed(session, proposed_database.config, seed_plan, "verify", confirm_database="rfq_copilot_test")  # fmt: skip
    assert (verified["status"], verified["initial_state"], verified["inserted"]) == (
        "PASS",
        "identical",
        0,
    )
    # Dataset v1.1.0's own 27 grants are untouched.
    stored = set((await session.execute(select(RolePermission.role, RolePermission.permission))).all())  # fmt: skip
    dataset = {(r["role"], r["permission"]) for r in seed_plan.rows["role_permissions"]}
    assert len(dataset) == 27 and dataset <= stored
    assert stored - dataset == grants(policy_rows("rbac-policy-v2"))


async def test_overlay_grants_operator_authority_only_to_identity_operator(session, proposed_database, seed_plan):  # fmt: skip
    await seed(session, proposed_database, seed_plan)
    await register_policy(session, "rbac-policy-v2")
    for permission in ("manage_customer_bindings", "revoke_tokens"):
        holders = set((await session.scalars(select(RolePermission.role).where(RolePermission.permission == permission))).all())  # fmt: skip
        assert holders == {"identity_operator"}
    operator_permissions = set((await session.scalars(select(RolePermission.permission).where(RolePermission.role == "identity_operator"))).all())  # fmt: skip
    assert operator_permissions == {"manage_customer_bindings", "revoke_tokens"}  # no API access
    assert await session.scalar(select(func.count()).select_from(User).where(User.role == "identity_operator")) == 0  # fmt: skip
    approval = {"request_approval", "approve_quote", "reject_quote", "release_quote"}
    assert not approval & {p for _, p in grants(policy_rows("rbac-policy-v2"))}


@pytest.mark.parametrize("damage", ["missing_grant", "allowed_false", "missing_role_grant", "extra_grant", "extra_permission"])  # fmt: skip
async def test_partial_modified_or_extended_overlay_fails_both_verifiers(session, proposed_database, seed_plan, damage):  # fmt: skip
    await seed(session, proposed_database, seed_plan)
    await register_policy(session, "rbac-policy-v2")
    grant = (RolePermission.role == "auditor") & (RolePermission.permission == "view_quote")
    if damage == "missing_grant":
        await session.execute(delete(RolePermission).where(grant))
    elif damage == "allowed_false":
        await session.execute(update(RolePermission).where(grant).values(allowed=False))
    elif damage == "missing_role_grant":
        await session.execute(delete(RolePermission).where(RolePermission.permission == "revoke_tokens"))  # fmt: skip
    elif damage == "extra_grant":  # an administrator given operator authority
        await session.execute(insert(RolePermission).values(role="administrator", permission="manage_customer_bindings", allowed=True))  # fmt: skip
    else:
        await session.execute(insert(Permission).values(permission="approve_everything"))
    with pytest.raises(IntegrityVerificationError):
        await audit_integrity(session, proposed_database.config, seed_plan)
    with pytest.raises(SeedError):
        await execute_seed(session, proposed_database.config, seed_plan, "verify", confirm_database="rfq_copilot_test")  # fmt: skip
    if damage in ("missing_grant", "allowed_false", "missing_role_grant"):
        with pytest.raises(SeedError):  # no silent repair of a damaged overlay
            await register_policy(session, "rbac-policy-v2")


async def test_overlay_refused_before_seeding(session):
    with pytest.raises(SeedError):
        await register_policy(session, "rbac-policy-v2")
    assert await session.scalar(select(func.count()).select_from(Role)) == 0


async def test_previous_contract_accepts_no_overlay(session, proposed_database, seed_plan, monkeypatch):  # fmt: skip
    await seed(session, proposed_database, seed_plan)
    await register_policy(session, "rbac-policy-v2")
    monkeypatch.setattr(schema_contract, "_ACTIVE", schema_contract.PREVIOUS)
    with pytest.raises(SeedError):  # the active contract refuses the overlay outright
        await register_policy(session, "rbac-policy-v2")
    assert not policy_module.is_policy_extra("roles", {"role": "identity_operator"})


async def test_tampered_or_unknown_policy_is_refused(session, proposed_database, seed_plan, tmp_path, monkeypatch):  # fmt: skip
    await seed(session, proposed_database, seed_plan)
    tampered = tmp_path / "policies"
    shutil.copytree(POLICIES_DIR, tampered)
    path = tampered / "rbac-policy-v2.json"
    path.write_bytes(path.read_bytes().replace(b'"auditor", "view_quote"', b'"administrator", "view_quote"'))  # fmt: skip
    assert hashlib.sha256(path.read_bytes()).hexdigest() != policy_module.TRUSTED_POLICIES["rbac-policy-v2"][1]  # fmt: skip
    monkeypatch.setattr(policy_module, "POLICIES_DIR", tampered)
    with pytest.raises(SeedError):
        await register_policy(session, "rbac-policy-v2")
    monkeypatch.undo()
    with pytest.raises(SeedError):
        await register_policy(session, "rbac-policy-v3")
    assert await session.scalar(select(func.count()).select_from(Role).where(Role.role == "identity_operator")) == 0  # fmt: skip


async def test_identity_operator_needs_the_schema_revision(session, proposed_database, seed_plan):
    await seed(session, proposed_database, seed_plan)
    revision = migration("20261111_06")

    def downgrade(sync_session):
        with Operations.context(MigrationContext.configure(sync_session.connection())):
            revision.downgrade()

    await register_policy(session, "rbac-policy-v2")
    savepoint = await session.begin_nested()
    with pytest.raises(CommandError):  # refused while the role is in use
        await session.run_sync(downgrade)
    await savepoint.rollback()
    for table in (RolePermission, Role):
        await session.execute(delete(table).where(table.role == "identity_operator"))
    await session.execute(delete(Permission).where(Permission.permission.in_(["manage_customer_bindings", "revoke_tokens"])))  # fmt: skip
    await session.run_sync(downgrade)  # now allowed: the baseline CHECK is restored
    savepoint = await session.begin_nested()
    with pytest.raises(Exception):  # noqa: B017 - the restored CHECK refuses the role
        await session.execute(insert(Role).values(role="identity_operator"))
    await savepoint.rollback()


def migration(revision):
    (path,) = (ROOT / "alembic" / "versions").glob(f"{revision}_*.py")
    spec = importlib.util.spec_from_file_location(f"lifecycle_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
