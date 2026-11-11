"""One real clean-test migration → source → seed → audit → queries → recovery lifecycle."""

import json

import pytest
from sqlalchemy import delete, func, select, text, update
from test_database_integrity import reject
from test_database_migrations import verify_physical
from test_database_seeding import ARCHIVE, SOURCE
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import session as session
from test_orm_models import approved as approved

from rfq_copilot.integrity import audit_integrity, fingerprint
from rfq_copilot.repositories.demo import verify_query_examples
from rfq_copilot.seeding.loader import execute_seed, insert_table
from rfq_copilot.seeding.mapping import TABLES, build_plan
from rfq_copilot.seeding.sources import load_sources

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def test_complete_day5_database_lifecycle(
    session, seed_database, approved, tmp_path, record_testsuite_property
):
    # Existing seed_database fixture proves the original empty test catalog,
    # migrates with Alembic, then guarded-downgrades/restores it in teardown.
    assert seed_database.config.target == "test"
    assert await session.scalar(text("SELECT current_database()")) == "rfq_copilot_test"
    connection = await session.connection()
    physical = await connection.run_sync(verify_physical, approved)
    assert physical["revision"] == ["20261111_06"]  # includes TD-012 and Phase 3.5
    assert len(physical["columns"]) == 36 and physical["counts"]["f"] == 71

    bundle = load_sources(SOURCE, ARCHIVE)
    plan = build_plan(bundle, tmp_path / "snapshots")
    first = await execute_seed(
        session, seed_database.config, plan, "apply", confirm_database="rfq_copilot_test"
    )
    assert first["inserted"] == 1089 and first["already_existing"] == 0
    integrity = await audit_integrity(session, seed_database.config, plan)
    assert integrity["status"] == "PASS" and integrity["source_values_checked"] == 9587
    second = await execute_seed(
        session, seed_database.config, plan, "apply", confirm_database="rfq_copilot_test"
    )
    assert second["inserted"] == 0 and second["already_existing"] == 1089
    assert first["after_digest"] == second["before_digest"] == second["after_digest"]

    queries = await verify_query_examples(session, seed_database.config)
    assert len(queries["examples"]) == 7 and all(
        r["status"] == "PASS" for r in queries["examples"].values()
    )
    assert all(queries["missing_identifiers"].values())
    assert queries["examples"]["pending_approvals"]["result"]["pending_requests"] == 0
    baseline = await fingerprint(session)

    customer = TABLES["customers"]
    contact = TABLES["contacts"]
    inventory = TABLES["material_inventory"]

    async def duplicate():
        await insert_table(session, "customers", [plan.rows["customers"][0]])

    async def invalid_foreign_key():
        await session.execute(
            update(contact)
            .where(contact.c.contact_id == plan.rows["contacts"][0]["contact_id"])
            .values(customer_id="E2E-MISSING")
        )

    async def invalid_check():
        await session.execute(
            update(inventory)
            .where(inventory.c.inventory_id == plan.rows["material_inventory"][0]["inventory_id"])
            .values(quantity_kg=-1)
        )

    async def restricted_delete():
        await session.execute(
            delete(customer).where(
                customer.c.customer_id == plan.rows["customers"][0]["customer_id"]
            )
        )

    async def partial_batch():
        await insert_table(
            session, "customers", [{**plan.rows["customers"][0], "customer_id": "E2E-ROLLBACK"}]
        )
        await insert_table(
            session,
            "contacts",
            [
                {
                    **plan.rows["contacts"][0],
                    "contact_id": "E2E-BAD-CONTACT",
                    "customer_id": "E2E-MISSING",
                }
            ],
        )

    for name, operation, state in [
        ("customers", duplicate, "23505"),
        ("contacts", invalid_foreign_key, "23503"),
        ("material_inventory", invalid_check, "23514"),
        ("customers", restricted_delete, "23001"),
        ("customers", partial_batch, "23503"),
    ]:
        await reject(session, name, operation, state)
    assert await fingerprint(session) == baseline

    # Entire fixture import remains rollback-only, allowing the approved cyclic
    # quote graph to be removed without deleting records or weakening constraints.
    await session.rollback()
    assert not session.in_transaction()
    async with seed_database.session() as observer:
        for table in TABLES.values():
            assert await observer.scalar(select(func.count()).select_from(table)) == 0
        assert await observer.scalar(text("SELECT 1")) == 1
    assert seed_database.engine.pool.checkedout() == 1  # Original lifecycle lease only.

    evidence = {
        "target": "rfq_copilot_test",
        "revision": physical["revision"],
        "physical_tables": 36,
        "source_integrity": bundle.integrity,
        "first_inserted": first["inserted"],
        "second_inserted": second["inserted"],
        "already_existing": second["already_existing"],
        "verified_values": integrity["source_values_checked"],
        "query_operations": len(queries["examples"]),
        "query_results": queries["examples"],
        "constraint_rejections": 5,
        "failure_fingerprint_preserved": True,
        "outer_transaction_rolled_back": True,
        "remaining_test_rows": 0,
        "session_released": True,
    }
    record_testsuite_property("day5_end_to_end", json.dumps(evidence, sort_keys=True))
