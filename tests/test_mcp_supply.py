"""Day 26 Supply MCP server, plus the Day 25 hardening checks (A1-A3) for all servers."""

import json
from contextlib import asynccontextmanager
from datetime import date, timedelta

import pytest
from mcp import Client
from sqlalchemy import text as sql
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day09_inventory import PANEL, pinned
from test_day10_capacity import set_free_hours
from test_mcp_manufacturing import SECRET, acting_as, text
from test_mcp_manufacturing import authenticated as authenticated

from rfq_copilot.entities import Part
from rfq_copilot.mcp import contracts as c
from rfq_copilot.mcp import supply as s
from rfq_copilot.mcp.manufacturing import create_manufacturing_server
from rfq_copilot.mcp.pricing import create_pricing_server
from rfq_copilot.mcp.supply import create_supply_server
from rfq_copilot.services.lead_time import estimate_lead_time

MONDAY = date(2026, 11, 9)  # first capacity week of the seeded snapshot
PLAN = {"parts": [PANEL], "horizon_start": MONDAY.isoformat()}
SERVERS = {
    "manufacturing": create_manufacturing_server,
    "supply": create_supply_server,
    "pricing": create_pricing_server,
}
PLANNING_TOOLS = [t.name for t in c.TOOLS if t.rfq_scoped]  # all supply + pricing tools
FACTORY = {t.name: SERVERS[t.server] for t in c.TOOLS}
INVALID = "INVALID_INPUT: Invalid tool arguments."


def spy():
    """A snapshot factory that records entries and yields no usable session."""
    entered = []

    @asynccontextmanager
    async def snapshot():
        entered.append(1)
        yield object()

    return snapshot, entered


def seeded(session, entered):
    @asynccontextmanager
    async def snapshot():
        entered.append(session)
        yield session

    return snapshot


async def call(server, name, request):
    async with Client(server) as client:
        return await client.call_tool(name, {"request": request})


# --- registration (no database) --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("server", ["supply", "pricing"])
async def test_each_server_registers_exactly_its_three_contract_tools(server):
    async with Client(SERVERS[server](spy()[0])) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    contracts = {t.name: t for t in c.TOOLS if t.server == server}
    assert len(tools) == 3 and set(tools) == set(contracts)
    for name, tool in tools.items():
        contract = contracts[name]
        assert tool.description == contract.description
        assert tool.annotations.read_only_hint and not tool.annotations.destructive_hint
        assert tool.input_schema["properties"]["request"]["$ref"].endswith(
            contract.input_model.__name__
        )
        assert set(tool.output_schema["properties"]) == set(contract.output_model.model_fields)


# --- fail closed, forged fields, sanitizing (wire path, no database) -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", PLANNING_TOOLS)
async def test_stored_rfq_fails_closed_without_any_lookup(name):
    # Day 27: stored RFQs are served only to a verified principal; without one, no lookup.
    snapshot, entered = spy()
    with acting_as(permissions=None):
        result = await call(
            FACTORY[name](snapshot), name, {"rfq_id": SECRET, "horizon_start": "2026-11-09"}
        )
    assert result.is_error and text(result) == "UNAUTHENTICATED: Authentication required."
    assert entered == []  # no session: nothing can reveal whether the RFQ exists
    assert SECRET not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", PLANNING_TOOLS)
@pytest.mark.parametrize(
    "arguments",
    [
        {"request": {**PLAN, "customer_id": SECRET}},
        {"request": {**PLAN, "role": SECRET}},
        {"request": {**PLAN, "inventory": {"result": "sufficient", "note": SECRET}}},
        {"request": {**PLAN, "costs": SECRET}},
        {"request": {**PLAN, "inventory_snapshot": SECRET}},
        {"request": {**PLAN, "pricing_rules": SECRET}},
        {"request": {**PLAN, "approval_rule_version": SECRET}},
        {"request": PLAN, "inventory": SECRET},  # top level: refused, not silently dropped
        {"request": {**PLAN, "parts": [{**PANEL, "thickness_mm": SECRET}]}},
        {"request": {**PLAN, "horizon_start": "2026-11-10"}},  # not a Monday
        {"request": SECRET},
        {},
        # A1: valid schema, rejected by the existing service rule (duplicate part_id)
        {"request": {**PLAN, "parts": [PANEL, {**PANEL, "quantity": 2}]}},
    ],
    ids=[
        "customer",
        "role",
        "inventory",
        "costs",
        "snapshot",
        "rules",
        "approval",
        "top-level",
        "bad-number",
        "weekday",
        "payload",
        "missing",
        "duplicate-part",
    ],
)
async def test_rejected_requests_never_open_a_snapshot_or_echo_input(name, arguments):
    snapshot, entered = spy()
    async with Client(FACTORY[name](snapshot)) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error and text(result) == INVALID
    assert entered == []
    assert SECRET not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_unknown_tool_name_is_sanitized():
    async with Client(create_supply_server(spy()[0])) as client:
        result = await client.call_tool(SECRET, {"request": PLAN})
    assert result.is_error and SECRET not in text(result) and text(result) == INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ValueError(SECRET), KeyError(SECRET), RuntimeError(f"/Users/x/.env {SECRET}")]
)
async def test_a1_unexpected_service_errors_are_internal(monkeypatch, failure):
    async def boom(*args, **kwargs):
        raise failure

    async def rules(session):
        return {}

    monkeypatch.setattr(s, "planning_rules", rules)
    monkeypatch.setattr(s, "estimate_lead_time", boom)
    async with Client(create_supply_server(spy()[0])) as client:
        result = await client.call_tool("estimate_lead_time", {"request": PLAN})
    assert text(result) == "INTERNAL: Internal error."
    assert SECRET not in json.dumps(result.model_dump(mode="json"))


# --- A2: production snapshot wiring on the guarded test database -----------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_a2_production_snapshot_is_one_read_only_repeatable_read_transaction(
    seed_database,
):
    # Migrated but unseeded as seen from a new connection: no data is created or changed.
    seen = []

    @asynccontextmanager
    async def snapshot():
        async with seed_database.snapshot_session() as session:
            seen.append(
                (
                    await session.scalar(sql("SHOW transaction_isolation")),
                    await session.scalar(sql("SHOW transaction_read_only")),
                )
            )
            yield session

    page = await call(create_manufacturing_server(snapshot), "search_materials", {})
    assert not page.is_error and page.structured_content["items"] == []
    no_rules = await call(create_supply_server(snapshot), "check_inventory", PLAN)
    assert text(no_rules) == "SNAPSHOT_CONFLICT: " + c.SAFE_MESSAGES[c.ErrorCode.SNAPSHOT_CONFLICT]
    assert seen == [("repeatable read", "on")] * 2  # exactly one snapshot per tool call


# --- seeded behaviour against the real services ----------------------------------------


@pytest.mark.parametrize(("quantity", "expected"), [(10, "sufficient"), (1000, "insufficient")])
@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_seeded_inventory_sufficiency_and_shortage(populated, quantity, expected):
    entered = []
    result = await call(
        create_supply_server(seeded(populated, entered)),
        "check_inventory",
        {**PLAN, "parts": [{**PANEL, "quantity": quantity}]},
    )
    assert not result.is_error, text(result)
    out = c.InventoryOutput.model_validate(result.structured_content)
    (line,) = out.lines
    assert out.result == line.status == expected and len(entered) == 1
    assert out.snapshot_id and out.pricing_rule_version == "price-v1.0"
    assert (line.shortage_kg > 0) == (expected == "insufficient")
    assert isinstance(result.structured_content["lines"][0]["required_kg"], str)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_seeded_capacity_feasible_then_infeasible(populated):
    server = create_supply_server(seeded(populated, []))
    feasible = c.CapacityOutput.model_validate(
        (await call(server, "check_capacity", PLAN)).structured_content
    )
    assert (feasible.result, feasible.reason, feasible.start_week) == ("feasible", None, 0)
    await set_free_hours(populated, "WC-QA", dict.fromkeys(range(10), 0))
    populated.expunge_all()
    blocked = c.CapacityOutput.model_validate(
        (await call(server, "check_capacity", PLAN)).structured_content
    )
    assert (blocked.result, blocked.reason) == ("infeasible", "NO_CAPACITY_IN_HORIZON")


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_seeded_capacity_reports_clarification_as_a_result(populated):
    part = {k: v for k, v in PANEL.items() if k != "thickness_mm"}
    result = await call(
        create_supply_server(seeded(populated, [])), "check_capacity", {**PLAN, "parts": [part]}
    )
    assert not result.is_error and result.structured_content["result"] == "needs_clarification"


@pytest.mark.parametrize("days", [0, 60])
@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_seeded_lead_time_matches_the_service_and_requested_delivery(populated, days):
    requested = MONDAY + timedelta(days=days)
    result = await call(
        create_supply_server(seeded(populated, [])),
        "estimate_lead_time",
        {**PLAN, "requested_delivery_date": requested.isoformat()},
    )
    out = c.LeadTimeOutput.model_validate(result.structured_content)
    direct = await estimate_lead_time(
        populated,
        [Part.from_mapping(PANEL)],
        **await pinned(populated),
        horizon_start=MONDAY,
        requested_delivery_date=requested,
    )
    assert out.result == direct.result == "estimated"
    assert (out.estimated_ready_date, out.lead_business_days, out.delivery_feasible) == (
        direct.estimated_ready_date,
        direct.lead_business_days,
        direct.delivery_feasible,
    )
    assert out.delivery_feasible is (days == 60)
    assert out.inventory_snapshot_id == direct.snapshots.inventory.snapshot_id
    assert out.capacity_snapshot_id == direct.snapshots.capacity.snapshot_id
