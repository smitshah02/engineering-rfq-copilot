"""Manufacturing MCP server (Day 25): four read-only catalog and feasibility tools.

Every tool requires a verified ``McpPrincipal`` holding ``view_catalog`` (enforced once in
``runtime.build_server``). No listener is created here; the Day 28 host passes the
``McpTokenVerifier`` that arms SDK bearer authentication.

Each call opens one session from ``snapshot`` (production: ``Database.snapshot_session``,
REPEATABLE READ + READ ONLY), loads the server-pinned manufacturing rules through the
sha256-checked ``get_rule_version``, calls the existing service once, and maps its frozen
result into the Day 24 output model. Business rejections are normal results.
"""

from mcp.server import MCPServer

from rfq_copilot.api.routers.materials import material_page
from rfq_copilot.api.schemas.reads import MaterialListQuery, MaterialPage
from rfq_copilot.mcp.contracts import (
    ErrorCode,
    GetMaterialInput,
    ManufacturingFeasibilityOutput,
    MaterialSpecOutput,
    MaterialSuitabilityOutput,
    PartCheckInput,
    tool_failure,
)
from rfq_copilot.mcp.runtime import (
    MANUFACTURING_RULE_VERSION,
    Snapshot,
    build_server,
    pinned_rules,
    to_parts,
)
from rfq_copilot.mcp.safety import safe_call
from rfq_copilot.mcp.security import McpPrincipal, McpTokenVerifier
from rfq_copilot.services.manufacturing import check_manufacturing_capability
from rfq_copilot.services.materials import check_part_material, get_material_spec


def create_manufacturing_server(
    snapshot: Snapshot, verifier: McpTokenVerifier | None = None
) -> MCPServer:
    """Build the server; the caller owns the database and passes its snapshot factory."""

    async def search_materials(request: MaterialListQuery, principal: McpPrincipal) -> MaterialPage:
        async def work():
            async with snapshot() as session:
                return await material_page(session, request)

        return await safe_call("search_materials", work)

    async def get_material(
        request: GetMaterialInput, principal: McpPrincipal
    ) -> MaterialSpecOutput:
        async def work():
            async with snapshot() as session:
                spec = await get_material_spec(session, request.material_id)
            if spec is None:
                raise tool_failure(ErrorCode.NOT_FOUND)
            return MaterialSpecOutput.model_validate(spec, from_attributes=True)

        return await safe_call("get_material", work)

    async def check_material_suitability(
        request: PartCheckInput, principal: McpPrincipal
    ) -> MaterialSuitabilityOutput:
        async def work():
            (part,) = to_parts([request.part])
            async with snapshot() as session:
                rules = await pinned_rules(session, MANUFACTURING_RULE_VERSION)
                check = await check_part_material(session, part, rules)
            return MaterialSuitabilityOutput.model_validate(check, from_attributes=True)

        return await safe_call("check_material_suitability", work)

    async def check_manufacturing_feasibility(
        request: PartCheckInput, principal: McpPrincipal
    ) -> ManufacturingFeasibilityOutput:
        async def work():
            (part,) = to_parts([request.part])
            async with snapshot() as session:  # the service runs the material check here too
                rules = await pinned_rules(session, MANUFACTURING_RULE_VERSION)
                check = await check_manufacturing_capability(session, part, rules)
            return ManufacturingFeasibilityOutput.model_validate(check, from_attributes=True)

        return await safe_call("check_manufacturing_feasibility", work)

    return build_server(
        "manufacturing",
        {
            "search_materials": search_materials,
            "get_material": get_material,
            "check_material_suitability": check_material_suitability,
            "check_manufacturing_feasibility": check_manufacturing_feasibility,
        },
        verifier,
    )
