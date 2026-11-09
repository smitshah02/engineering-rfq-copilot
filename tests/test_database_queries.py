"""Real async repository reads, scoped test data and development preservation."""

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session

from rfq_copilot.database import Database
from rfq_copilot.integrity import fingerprint
from rfq_copilot.repositories import (
    get_customer,
    get_manufacturing_capabilities,
    get_material_availability,
    get_rfq,
    get_workcentre_capacity,
    list_pending_approvals,
    list_quotation_history,
)
from rfq_copilot.seeding.loader import execute_seed
from rfq_copilot.seeding.mapping import TABLES

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(loop_scope="module")
async def populated(session, seed_database, seed_plan):
    await execute_seed(
        session, seed_database.config, seed_plan, "apply", confirm_database="rfq_copilot_test"
    )
    return session


@contextmanager
def statements(database):
    captured = []

    def trace(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)  # No parameter values or business payloads.

    event.listen(database.engine.sync_engine, "before_cursor_execute", trace)
    try:
        yield captured
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", trace)


def customer_id(plan):
    return plan.rows["rfqs"][0]["customer_id"]


def other_customer(plan):
    return next(
        c["customer_id"] for c in plan.rows["customers"] if c["customer_id"] != customer_id(plan)
    )


async def test_customer_contacts_are_exact_and_scoped(populated, seed_plan):
    identifier = customer_id(seed_plan)
    result = await get_customer(populated, identifier)
    assert result.customer.customer_id == identifier
    assert {c.contact_id for c in result.contacts} == {
        c["contact_id"] for c in seed_plan.rows["contacts"] if c["customer_id"] == identifier
    }
    assert all(c.customer_id == identifier for c in result.contacts)
    assert isinstance(result.customer.credit_limit_usd, Decimal)
    assert await get_customer(populated, "MISSING-CUSTOMER") is None


async def test_contact_ordering_does_not_modify_relationships(populated, seed_plan):
    base = seed_plan.rows["contacts"][0]
    await populated.execute(
        insert(TABLES["contacts"]).values(
            {**base, "contact_id": "TEST-Z", "contact_name": "ZZZ", "is_primary": False}
        )
    )
    await populated.execute(
        insert(TABLES["contacts"]).values(
            {**base, "contact_id": "TEST-A", "contact_name": "AAA", "is_primary": False}
        )
    )
    result = await get_customer(populated, base["customer_id"])
    assert [(c.contact_name, c.contact_id) for c in result.contacts] == sorted(
        (c.contact_name, c.contact_id) for c in result.contacts
    )
    assert not populated.dirty


async def test_historical_rfq_preserves_part_references_without_runtime_lines(populated, seed_plan):
    source = seed_plan.rows["rfqs"][0]
    result = await get_rfq(populated, source["rfq_id"], customer_id=source["customer_id"])
    assert (
        result.rfq.rfq_id == source["rfq_id"]
        and result.customer.customer_id == source["customer_id"]
    )
    assert result.historical_part_ids == tuple(source["part_ids"])
    assert result.runtime_lines == ()
    assert result.rfq.source_type == "historical_internal"
    assert await get_rfq(populated, source["rfq_id"], customer_id=other_customer(seed_plan)) is None
    assert await get_rfq(populated, "MISSING-RFQ", customer_id=source["customer_id"]) is None


async def test_runtime_rfq_lines_remain_distinct_from_historical_ids(populated, seed_plan):
    identifier = customer_id(seed_plan)
    await populated.execute(
        insert(TABLES["rfqs"]).values(
            rfq_id="TEST-RUNTIME-RFQ", customer_id=identifier, source_type="mcp_shell"
        )
    )
    line_id = uuid4()
    await populated.execute(
        insert(TABLES["rfq_line_items"]).values(
            rfq_line_id=line_id,
            rfq_id="TEST-RUNTIME-RFQ",
            line_number=1,
            part_id="TEST-RUNTIME-PART",
            quantity=2,
        )
    )
    result = await get_rfq(populated, "TEST-RUNTIME-RFQ", customer_id=identifier)
    assert result.historical_part_ids == ()
    assert len(result.runtime_lines) == 1 and result.runtime_lines[0].rfq_line_id == line_id


async def test_material_inventory_decimal_totals_and_thickness_filter(populated, seed_plan):
    material = seed_plan.rows["materials"][0]["material_id"]
    expected = [r for r in seed_plan.rows["material_inventory"] if r["material_id"] == material]
    result = await get_material_availability(populated, material)
    assert {r.inventory_id for r in result.inventory} == {r["inventory_id"] for r in expected}
    assert result.available_kg == sum((r["available_kg"] for r in expected), Decimal(0))
    assert all(
        isinstance(r.available_kg, Decimal) and r.available_kg == r.quantity_kg - r.reserved_kg
        for r in result.inventory
    )
    thickness = expected[0]["thickness_mm"]
    filtered = await get_material_availability(populated, material, thickness_mm=thickness)
    assert all(r.thickness_mm == thickness for r in filtered.inventory)
    empty = await get_material_availability(populated, material, thickness_mm=Decimal("7.5"))
    assert empty.inventory == () and empty.available_kg == Decimal(0)
    assert await get_material_availability(populated, "MISSING-MATERIAL") is None


async def test_inventory_available_and_zero_filters(populated, seed_plan):
    row = seed_plan.rows["material_inventory"][0]
    await populated.execute(
        update(TABLES["material_inventory"])
        .where(TABLES["material_inventory"].c.inventory_id == row["inventory_id"])
        .values(quantity_kg=Decimal(0), reserved_kg=Decimal(0))
    )
    empty = await get_material_availability(
        populated, row["material_id"], availability="unavailable"
    )
    available = await get_material_availability(
        populated, row["material_id"], availability="available"
    )
    assert row["inventory_id"] in {r.inventory_id for r in empty.inventory}
    assert all(r.available_kg == 0 for r in empty.inventory)
    assert all(r.available_kg > 0 for r in available.inventory)
    assert not {r.inventory_id for r in empty.inventory} & {
        r.inventory_id for r in available.inventory
    }


async def test_stored_manufacturing_capabilities_and_routing(populated, seed_plan):
    material = seed_plan.rows["materials"][0]["material_id"]
    result = await get_manufacturing_capabilities(populated, material, part_type="flat_panel")
    operations = {
        r["operation_id"]
        for r in seed_plan.rows["material_supported_operations"]
        if r["material_id"] == material
    }
    assert {r.operation_id for r in result.operations} == operations
    assert set(result.supported_thickness_mm) == {
        r["thickness_mm"]
        for r in seed_plan.rows["material_thickness_options"]
        if r["material_id"] == material
    }
    assert {f.finish_id for f in result.finishes} == {
        r["finish_id"]
        for r in seed_plan.rows["material_finish_compatibility"]
        if r["material_id"] == material
    }
    assert {r.routing_id for r in result.routing} == {
        r["routing_id"]
        for r in seed_plan.rows["operation_routing"]
        if r["part_type"] == "flat_panel" and r["operation_id"] in operations
    }
    centres = {w.workcentre_id: w for w in result.workcentres}
    assert all(centres[r.workcentre_id].operation_id == r.operation_id for r in result.routing)
    assert await get_manufacturing_capabilities(populated, "MISSING-MATERIAL") is None


async def test_workcentre_capacity_week_range_and_hours(populated, seed_plan):
    centre = seed_plan.rows["workcentres"][0]
    start = date(2026, 11, 9)
    result = await get_workcentre_capacity(
        populated, centre["workcentre_id"], week_start=start, week_end=start + timedelta(weeks=1)
    )
    assert [r.week_start for r in result.weeks] == [start, start + timedelta(weeks=1)]
    assert result.remaining_hours == sum((r.remaining_hours for r in result.weeks), Decimal(0))
    assert result.workcentre.operation.operation_id == centre["operation_id"]
    assert all(
        isinstance(r.remaining_hours, Decimal)
        and r.remaining_hours == r.nominal_hours - r.downtime_hours - r.scheduled_hours
        for r in result.weeks
    )
    empty = await get_workcentre_capacity(
        populated, centre["workcentre_id"], week_start=start + timedelta(weeks=20)
    )
    assert empty.weeks == () and empty.remaining_hours == Decimal(0)
    assert await get_workcentre_capacity(populated, "MISSING-WORKCENTRE") is None


async def test_quotation_history_all_versions_lines_ordering_and_scope(populated, seed_plan):
    identifier = customer_id(seed_plan)
    rfqs = {r["rfq_id"] for r in seed_plan.rows["rfqs"] if r["customer_id"] == identifier}
    expected = [q for q in seed_plan.rows["quotes"] if q["rfq_id"] in rfqs]
    result = await list_quotation_history(populated, identifier)
    assert {r.quote.quote_id for r in result} == {q["quote_id"] for q in expected}
    assert len(result) == len({r.quote.quote_id for r in result})
    assert [r.quote.quote_id for r in result] == [
        q["quote_id"]
        for q in sorted(expected, key=lambda q: (-q["created_at"].timestamp(), q["quote_id"]))
    ]
    for quote in result:
        assert quote.quote.rfq.customer_id == identifier
        expected_versions = [
            v for v in seed_plan.rows["quote_versions"] if v["quote_id"] == quote.quote.quote_id
        ]
        assert [v.version.version for v in quote.versions] == sorted(
            v["version"] for v in expected_versions
        )
        for version in quote.versions:
            assert version.version.quote_id == quote.quote.quote_id
            assert [line.line_number for line in version.line_items] == sorted(
                line.line_number for line in version.line_items
            )
            assert all(
                line.quote_version == version.version.version
                and line.quote_id == version.version.quote_id
                for line in version.line_items
            )
            if version.version.snapshot_completeness == "metadata_only":
                assert version.version.total_usd is None and version.line_items == ()
            else:
                assert (
                    sum((line.line_total_usd for line in version.line_items), Decimal(0))
                    == version.version.total_usd
                )
                assert all(
                    isinstance(line.line_total_usd, Decimal)
                    and isinstance(line.cost_breakdown_json["line_total_usd"], str)
                    for line in version.line_items
                )
    rfq = seed_plan.rows["rfqs"][0]
    assert len(await list_quotation_history(populated, identifier, rfq_id=rfq["rfq_id"])) == 1
    assert (
        await list_quotation_history(populated, other_customer(seed_plan), rfq_id=rfq["rfq_id"])
        == ()
    )
    assert await list_quotation_history(populated, "MISSING-CUSTOMER") == ()


async def test_pending_empty_does_not_reconstruct_historical_events(populated, seed_plan):
    assert (
        await populated.scalar(select(func.count()).select_from(TABLES["approval_history"])) == 70
    )
    assert await list_pending_approvals(populated, customer_id=customer_id(seed_plan)) == ()


async def test_pending_requests_scope_status_role_and_order(populated, seed_plan):
    chosen = next(q for q in seed_plan.rows["quotes"] if q["status"] == "pending_approval")
    rfqs = {r["rfq_id"]: r for r in seed_plan.rows["rfqs"]}
    identifier = rfqs[chosen["rfq_id"]]["customer_id"]
    own = [q for q in seed_plan.rows["quotes"] if rfqs[q["rfq_id"]]["customer_id"] == identifier]
    other = next(
        q for q in seed_plan.rows["quotes"] if rfqs[q["rfq_id"]]["customer_id"] != identifier
    )
    when = datetime(2026, 11, 2, 12, tzinfo=UTC)
    rows = []
    for quote, key in [
        (own[0], "TEST-PENDING-B"),
        (own[1], "TEST-PENDING-A"),
        (other, "TEST-OTHER"),
    ]:
        rows.append(
            {
                "approval_request_id": key,
                "quote_id": quote["quote_id"],
                "quote_version": quote["current_version"],
                "requested_by": quote["created_by"],
                "requested_at": when,
                "approval_rule_version": "approval-v1.1",
            }
        )
    await populated.execute(insert(TABLES["approval_requests"]), rows)
    manager = next(u["user_id"] for u in seed_plan.rows["users"] if u["role"] == "sales_manager")
    await populated.execute(
        insert(TABLES["approval_requests"]).values(
            {
                **rows[0],
                "approval_request_id": "TEST-RESOLVED",
                "status": "approved",
                "resolved_by": manager,
                "resolved_at": when,
            }
        )
    )
    result = await list_pending_approvals(
        populated, customer_id=identifier, required_role="sales_manager"
    )
    assert [r.approval_request_id for r in result] == ["TEST-PENDING-A", "TEST-PENDING-B"]
    assert all(r.status == "pending" and r.required_role == "sales_manager" for r in result)
    assert await list_pending_approvals(populated, customer_id="MISSING-CUSTOMER") == ()


OPERATIONS = ["customer", "rfq", "inventory", "capabilities", "capacity", "history", "approvals"]


async def call(name, session, plan, identifier=None):
    customer = customer_id(plan) if plan else "CUST-001"
    if name == "customer":
        return await get_customer(session, customer if identifier is None else identifier)
    if name == "rfq":
        return await get_rfq(
            session,
            (plan.rows["rfqs"][0]["rfq_id"] if plan else "HRFQ-0001")
            if identifier is None
            else identifier,
            customer_id=customer,
        )
    if name == "inventory":
        return await get_material_availability(
            session, "MAT-001" if identifier is None else identifier
        )
    if name == "capabilities":
        return await get_manufacturing_capabilities(
            session, "MAT-001" if identifier is None else identifier
        )
    if name == "capacity":
        return await get_workcentre_capacity(
            session,
            (plan.rows["workcentres"][0]["workcentre_id"] if plan else "WC-001")
            if identifier is None
            else identifier,
        )
    if name == "history":
        return await list_quotation_history(session, customer if identifier is None else identifier)
    return await list_pending_approvals(
        session, customer_id=customer if identifier is None else identifier
    )


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("identifier", ["", 123])
async def test_invalid_identifiers_fail_before_sql(operation, identifier):
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await call(operation, session, None, identifier)
    session.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "operation,expected_selects", list(zip(OPERATIONS, [2, 2, 2, 6, 2, 3, 1], strict=True))
)
async def test_query_counts_are_bounded_without_lazy_n_plus_one(
    populated, seed_database, seed_plan, operation, expected_selects
):
    populated.expunge_all()
    with statements(seed_database) as captured:
        result = await call(operation, populated, seed_plan)
        assert result is not None
        if operation == "customer":
            assert all(c.contact_id for c in result.contacts)
        elif operation == "history":
            assert all(v.version.quote_version_id for q in result for v in q.versions)
            assert all(line.part_id for q in result for v in q.versions for line in v.line_items)
    assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
    assert len(captured) == expected_selects


@pytest.mark.parametrize(
    "thickness", [1.0, "1.0", Decimal("NaN"), Decimal("1.0001"), Decimal(0), Decimal(-1)]
)
async def test_bad_thickness_inputs(thickness):
    with pytest.raises(ValueError):
        await get_material_availability(
            AsyncMock(spec=AsyncSession), "MAT-001", thickness_mm=thickness
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"week_start": date(2026, 11, 10)},
        {"week_start": datetime(2026, 11, 9, tzinfo=UTC)},
        {"week_start": date(2026, 11, 16), "week_end": date(2026, 11, 9)},
    ],
)
async def test_invalid_week_filters(arguments):
    with pytest.raises(ValueError):
        await get_workcentre_capacity(AsyncMock(spec=AsyncSession), "WC-001", **arguments)


async def test_invalid_status_role_and_part_type_filters():
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await get_material_availability(session, "MAT-001", availability="guess")
    with pytest.raises(ValueError):
        await get_manufacturing_capabilities(session, "MAT-001", part_type="casting")
    with pytest.raises(ValueError):
        await list_pending_approvals(session, customer_id="CUST-001", required_role="auditor")


async def test_bound_identifier_cannot_inject_sql(populated, seed_database):
    identifier = "CUST-001' OR 1=1 --"
    with statements(seed_database) as captured:
        assert await get_customer(populated, identifier) is None
    assert all(identifier not in statement for statement in captured)
    assert await populated.scalar(select(func.count()).select_from(TABLES["customers"])) == 40


async def test_reads_do_not_autoflush_pending_caller_changes(populated, seed_database, seed_plan):
    result = await get_customer(populated, customer_id(seed_plan))
    original = result.customer.company_name
    result.customer.company_name = "unflushed caller edit"
    populated.autoflush = True
    try:
        with statements(seed_database) as captured:
            await get_customer(populated, result.customer.customer_id)
            await list_quotation_history(populated, result.customer.customer_id)
        assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
    finally:
        populated.autoflush = False
        result.customer.company_name = original


async def test_all_development_reads_preserve_records_and_close_sessions(
    development_config, seed_plan
):
    async with Database(development_config, read_only=True) as database:
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                before = await fingerprint(session)
                with statements(database) as captured:
                    for operation in OPERATIONS:
                        await call(operation, session, seed_plan)
                assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
                assert await fingerprint(session) == before
                assert sum(t["rows"] for t in before["tables"].values()) == 1089
                assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        assert database.engine.pool.checkedout() == 0
