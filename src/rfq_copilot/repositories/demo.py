"""Execute seven representative reads and emit only safe counts/identifiers/units."""

import argparse
import asyncio
import json
import time
from datetime import date
from pathlib import Path

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
from rfq_copilot.database import Database, DatabaseError
from rfq_copilot.integrity import IntegrityVerificationError, check, fingerprint
from rfq_copilot.models import RFQ, Material, Workcentre
from rfq_copilot.repositories import (
    get_customer,
    get_manufacturing_capabilities,
    get_material_availability,
    get_rfq,
    get_workcentre_capacity,
    list_pending_approvals,
    list_quotation_history,
)
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.safety import validate_target, verify_schema


async def verify_query_examples(session: AsyncSession, config: DatabaseConfig):
    """Run the seven real reads in a caller-owned transaction; never commit or write."""
    validate_target(config)
    examples = {}
    connection = await session.connection()
    identity = await connection.run_sync(verify_schema, config)
    before = await fingerprint(session)
    rfq_id, customer_id = (
        await session.execute(select(RFQ.rfq_id, RFQ.customer_id).order_by(RFQ.rfq_id).limit(1))
    ).one()
    material_id = await session.scalar(
        select(Material.material_id).order_by(Material.material_id).limit(1)
    )
    workcentre_id = await session.scalar(
        select(Workcentre.workcentre_id).order_by(Workcentre.workcentre_id).limit(1)
    )

    async def observe(name, inputs, query, summary):
        statements = []

        def trace(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(connection.sync_connection, "before_cursor_execute", trace)
        started = time.monotonic()
        try:
            result = await query()
            safe_summary = summary(result)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", trace)
        check(
            all(s.lstrip().upper().startswith("SELECT") for s in statements),
            name,
            "unexpected write",
        )
        examples[name] = {
            "input": inputs,
            "result": safe_summary,
            "selects": len(statements),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "status": "PASS",
        }

    await observe(
        "customer",
        {"customer_id": customer_id},
        lambda: get_customer(session, customer_id),
        lambda r: {"customer_id": r.customer.customer_id, "contacts": len(r.contacts)},
    )
    await observe(
        "rfq",
        {"rfq_id": rfq_id, "customer_id": customer_id},
        lambda: get_rfq(session, rfq_id, customer_id=customer_id),
        lambda r: {
            "rfq_id": r.rfq.rfq_id,
            "historical_part_ids": len(r.historical_part_ids),
            "runtime_lines": len(r.runtime_lines),
        },
    )
    await observe(
        "material_availability",
        {"material_id": material_id, "availability": "all"},
        lambda: get_material_availability(session, material_id),
        lambda r: {
            "inventory_rows": len(r.inventory),
            "available_kg": str(r.available_kg),
        },
    )
    await observe(
        "manufacturing_capabilities",
        {"material_id": material_id, "part_type": "flat_panel"},
        lambda: get_manufacturing_capabilities(session, material_id, part_type="flat_panel"),
        lambda r: {
            "operations": len(r.operations),
            "workcentres": len(r.workcentres),
            "routing_rows": len(r.routing),
            "finishes": len(r.finishes),
            "thickness_options": len(r.supported_thickness_mm),
        },
    )
    week = date(2026, 11, 9)
    await observe(
        "workcentre_capacity",
        {
            "workcentre_id": workcentre_id,
            "week_start": week.isoformat(),
            "week_end": week.isoformat(),
        },
        lambda: get_workcentre_capacity(session, workcentre_id, week_start=week, week_end=week),
        lambda r: {"weeks": len(r.weeks), "remaining_hours": str(r.remaining_hours)},
    )
    await observe(
        "quotation_history",
        {"customer_id": customer_id, "rfq_id": rfq_id},
        lambda: list_quotation_history(session, customer_id, rfq_id=rfq_id),
        lambda r: {
            "quotations": len(r),
            "versions": sum(len(q.versions) for q in r),
            "line_items": sum(len(v.line_items) for q in r for v in q.versions),
            "metadata_only_versions": sum(
                v.version.snapshot_completeness == "metadata_only" for q in r for v in q.versions
            ),
        },
    )
    await observe(
        "pending_approvals",
        {"customer_id": customer_id, "required_role": "sales_manager"},
        lambda: list_pending_approvals(session, customer_id=customer_id),
        lambda r: {"pending_requests": len(r)},
    )

    missing = {
        "customer": await get_customer(session, "MISSING-CUSTOMER") is None,
        "rfq": await get_rfq(session, "MISSING-RFQ", customer_id=customer_id) is None,
        "material_availability": await get_material_availability(session, "MISSING-MATERIAL")
        is None,
        "manufacturing_capabilities": await get_manufacturing_capabilities(
            session, "MISSING-MATERIAL"
        )
        is None,
        "workcentre_capacity": await get_workcentre_capacity(session, "MISSING-WORKCENTRE") is None,
        "quotation_history": await list_quotation_history(session, "MISSING-CUSTOMER") == (),
        "pending_approvals": await list_pending_approvals(session, customer_id="MISSING-CUSTOMER")
        == (),
    }
    check(all(missing.values()), "queries", "missing-identifier behavior differs")
    after = await fingerprint(session)
    check(before == after, "queries", "database fingerprint changed")
    result = {
        "status": "PASS",
        **identity,
        "examples": examples,
        "missing_identifiers": missing,
        "before_fingerprint": before,
        "after_fingerprint": after,
        "read_only": await session.scalar(text("SHOW transaction_read_only")) == "on",
    }
    return result


async def run_examples(config):
    validate_target(config)
    async with Database(config, read_only=True) as database:
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                result = await verify_query_examples(session, config)
        check(database.engine.pool.checkedout() == 0, "queries", "session was not released")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Execute representative scoped read-only queries.")
    parser.add_argument("--target", required=True, choices=["development", "test"])
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                asyncio.run(
                    run_examples(DatabaseConfig.from_env(args.target, env_file=Path(args.env_file)))
                ),
                sort_keys=True,
            )
        )
        return 0
    except (
        DatabaseConfigurationError,
        DatabaseError,
        IntegrityVerificationError,
        SeedError,
    ) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
