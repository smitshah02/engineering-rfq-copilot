"""Supply MCP server (Day 26): inventory, capacity and lead time. Read-only, in-process.

Agents never supply upstream results. Each call recomputes what its service needs inside
ONE snapshot session, using the existing orchestration: ``select_snapshots`` (trusted
inventory/capacity snapshot choice and planning-date policy), then ``check_inventory``,
and for capacity ``estimate_lead_time`` (which derives the capacity start week from the
procurement delay exactly as quotations do). Stored ``rfq_id`` requests are loaded customer-scoped inside the same snapshot
(``runtime.planning_parts``); tools require ``view_assigned_rfqs``.
"""

from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.mcp.contracts import (
    CapacityOutput,
    DeliveryPlanningInput,
    InventoryOutput,
    LeadTimeOutput,
    PlanningInput,
)
from rfq_copilot.mcp.runtime import (
    Snapshot,
    build_server,
    planning_parts,
    planning_rules,
    project,
    prospective_parts,
)
from rfq_copilot.mcp.safety import safe_call
from rfq_copilot.mcp.security import McpPrincipal, McpTokenVerifier
from rfq_copilot.services.capacity import check_capacity
from rfq_copilot.services.inventory import InventoryCheck, check_inventory
from rfq_copilot.services.lead_time import estimate_lead_time, select_snapshots


async def trusted_inventory(
    session: AsyncSession, parts: tuple[Part, ...], rules: dict, request: PlanningInput
) -> InventoryCheck:
    """Inventory against the server-selected snapshot (also used by the pricing server)."""
    selection = await select_snapshots(session, horizon_start=request.horizon_start)
    return await check_inventory(session, parts, **rules, snapshot=selection.inventory)


def create_supply_server(snapshot: Snapshot, verifier: McpTokenVerifier | None = None) -> MCPServer:
    async def check_inventory_tool(
        request: PlanningInput, principal: McpPrincipal
    ) -> InventoryOutput:
        async def work():
            prospective = prospective_parts(request)
            async with snapshot() as session:
                parts, numbers = await planning_parts(session, request, principal, prospective)
                rules = await planning_rules(session)
                inventory = await trusted_inventory(session, parts, rules, request)
            return project(InventoryOutput, inventory)

        return await safe_call("check_inventory", work)

    async def check_capacity_tool(
        request: PlanningInput, principal: McpPrincipal
    ) -> CapacityOutput:
        async def work():
            prospective = prospective_parts(request)
            async with snapshot() as session:
                parts, numbers = await planning_parts(session, request, principal, prospective)
                rules = await planning_rules(session)
                lead = await estimate_lead_time(
                    session, parts, **rules, horizon_start=request.horizon_start
                )
                capacity = lead.capacity
                if capacity is None:
                    # Inventory was needs_clarification / not_manufacturable, so lead time
                    # skipped capacity; the service reports that state without data reads.
                    capacity = await check_capacity(
                        session,
                        parts,
                        lead.inventory,
                        **rules,
                        horizon_start=request.horizon_start,
                        snapshot=lead.snapshots.capacity if lead.snapshots else None,
                    )
            return project(CapacityOutput, capacity)

        return await safe_call("check_capacity", work)

    async def estimate_lead_time_tool(
        request: DeliveryPlanningInput, principal: McpPrincipal
    ) -> LeadTimeOutput:
        async def work():
            prospective = prospective_parts(request)
            async with snapshot() as session:
                parts, numbers = await planning_parts(session, request, principal, prospective)
                rules = await planning_rules(session)
                lead = await estimate_lead_time(
                    session,
                    parts,
                    **rules,
                    horizon_start=request.horizon_start,
                    requested_delivery_date=request.requested_delivery_date,
                )
            pins = lead.snapshots
            return project(
                LeadTimeOutput,
                lead,
                inventory_snapshot_id=pins.inventory.snapshot_id if pins else None,
                capacity_snapshot_id=pins.capacity.snapshot_id if pins else None,
            )

        return await safe_call("estimate_lead_time", work)

    return build_server(
        "supply",
        {
            "check_inventory": check_inventory_tool,
            "check_capacity": check_capacity_tool,
            "estimate_lead_time": estimate_lead_time_tool,
        },
        verifier,
    )
