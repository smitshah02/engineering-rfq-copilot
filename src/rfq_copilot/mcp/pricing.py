"""Pricing MCP server (Day 26): costs, price and multi-part quotation evaluation.

Read-only, in-process, nothing persisted, approved or released. Each call recomputes its
prerequisites inside ONE snapshot session: inventory (server-selected snapshot) then
``estimate_costs``; then ``calculate_quote``; ``evaluate_quotation`` is ``quote_parts``,
which runs lead time, costs and pricing itself. No tool here needs approval rules: the
commercial-policy tool (``evaluate_stored_rfq`` under ``approval-v1.2``) stays deferred.
Stored ``rfq_id`` requests are loaded customer-scoped inside the same snapshot
(``runtime.planning_parts``); tools require ``view_quote``.
"""

from mcp.server import MCPServer

from rfq_copilot.mcp.contracts import (
    CostOutput,
    DeliveryPlanningInput,
    PlanningInput,
    PriceOutput,
    QuotationOutput,
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
from rfq_copilot.mcp.supply import trusted_inventory
from rfq_copilot.services.costing import estimate_costs
from rfq_copilot.services.pricing import calculate_quote
from rfq_copilot.services.quotation import quote_parts


def create_pricing_server(
    snapshot: Snapshot, verifier: McpTokenVerifier | None = None
) -> MCPServer:
    async def estimate_costs_tool(request: PlanningInput, principal: McpPrincipal) -> CostOutput:
        async def work():
            prospective = prospective_parts(request)
            async with snapshot() as session:
                parts, numbers = await planning_parts(session, request, principal, prospective)
                rules = await planning_rules(session)
                inventory = await trusted_inventory(session, parts, rules, request)
                costs = await estimate_costs(session, parts, inventory, **rules)
            return project(CostOutput, costs)

        return await safe_call("estimate_costs", work)

    async def calculate_price_tool(request: PlanningInput, principal: McpPrincipal) -> PriceOutput:
        async def work():
            prospective = prospective_parts(request)
            async with snapshot() as session:
                parts, numbers = await planning_parts(session, request, principal, prospective)
                rules = await planning_rules(session)
                inventory = await trusted_inventory(session, parts, rules, request)
                costs = await estimate_costs(session, parts, inventory, **rules)
                price = await calculate_quote(
                    session, parts, costs, pricing_rules=rules["pricing_rules"]
                )
            lines = [
                {
                    "part_id": line.cost.part_id,
                    "quoted_unit_price_usd": line.quoted_unit_price_usd,
                    "line_total_usd": line.line_total_usd,
                }
                for line in price.lines
            ]
            return project(PriceOutput, price, lines=lines)

        return await safe_call("calculate_price", work)

    async def evaluate_quotation_tool(
        request: DeliveryPlanningInput, principal: McpPrincipal
    ) -> QuotationOutput:
        async def work():
            prospective = prospective_parts(request)
            async with snapshot() as session:
                parts, numbers = await planning_parts(session, request, principal, prospective)
                rules = await planning_rules(session)
                quotation = await quote_parts(
                    session,
                    parts,
                    **rules,
                    horizon_start=request.horizon_start,
                    requested_delivery_date=request.requested_delivery_date,
                    line_numbers=numbers,
                )
            lines = [
                {
                    "line_number": line.line_number,
                    "part_id": line.part_id,
                    "manufacturing_result": line.manufacturing.result,
                    "findings": line.manufacturing.findings,
                    "stock_status": line.stock_status,
                    "procurement_business_days": line.procurement_business_days,
                    "line_total_usd": line.priced.line_total_usd if line.priced else None,
                }
                for line in quotation.lines
            ]
            return project(QuotationOutput, quotation, lines=lines)

        return await safe_call("evaluate_quotation", work)

    return build_server(
        "pricing",
        {
            "estimate_costs": estimate_costs_tool,
            "calculate_price": calculate_price_tool,
            "evaluate_quotation": evaluate_quotation_tool,
        },
        verifier,
    )
