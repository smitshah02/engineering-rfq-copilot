"""Day 39 opt-in live check: A1 -> A2 -> A3 through the real MCP host and seeded services.

``RFQ_AGENT_LIVE_MCP=1`` (marked ``integration``), same fixtures as the Day 37/38 live tests:
a separate uvicorn MCP host on loopback TCP over the guarded, seeded test database. No model.
The planning horizon is the Monday the Day 34 live tests use as trusted policy.
"""

import os
from datetime import date

import pytest
from test_auth import AUDIENCE as REST_AUDIENCE
from test_auth import trust as trust
from test_intake import proposal
from test_intake_validation import COMPLETE, complete_args
from test_mcp_auth import MCP_AUDIENCE
from test_mcp_client import TestIssuer
from test_mcp_live_e2e import live as live
from test_mcp_live_e2e import seed_bundle as seed_bundle
from test_mcp_live_e2e import seed_database as seed_database
from test_mcp_live_e2e import seed_plan as seed_plan
from test_mcp_live_e2e import session as session

from rfq_copilot import graph as g
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.mcp_integration import McpIntegration
from rfq_copilot.intake import verify_extraction
from rfq_copilot.intake_validation import validate_intake
from rfq_copilot.manufacturing_agent import evaluate_manufacturing_feasibility
from rfq_copilot.mcp.client import McpToolClient
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION, PRICING_RULE_VERSION, pinned_rules
from rfq_copilot.supply_agent import evaluate_supply

HORIZON = date(2026, 11, 9)


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RFQ_AGENT_LIVE_MCP") != "1", reason="opt-in: RFQ_AGENT_LIVE_MCP=1")  # fmt: skip
@pytest.mark.asyncio(loop_scope="module")
async def test_supply_agent_through_the_live_mcp_host(trust, live, seed_database):
    from test_mcp_client import live_host

    async with seed_database.session() as s:
        rules = await pinned_rules(s, MANUFACTURING_RULE_VERSION)
    extraction = verify_extraction(proposal(complete_args()), COMPLETE, "live-src", "RFQ-LIVE")
    with live_host(trust, RFQ_MCP_HOST_RATE_LIMIT_PER_MINUTE="1000") as (base, secrets, log):
        integration = McpIntegration(McpToolClient(base, timeout_seconds=20), TestIssuer(trust), MCP_AUDIENCE, REST_AUDIENCE)  # fmt: skip

        def executor(name):
            principal = Principal("LIVE", frozenset(), user_id="LIVE", subject=live["subjects"][name])  # fmt: skip
            return g.mcp_executor(integration, principal, trust.mint(sub=live["subjects"][name]))

        intake = await validate_intake(extraction, COMPLETE, rules=rules, catalog=executor("a"))
        a2 = await evaluate_manufacturing_feasibility(intake, executor("a"))
        ok = await evaluate_supply(a2, executor("a"), horizon_start=HORIZON, extraction=extraction)
        revoked = await evaluate_supply(a2, executor("revoked"), horizon_start=HORIZON)
    assert a2.status == "feasible", [p.explanation for p in a2.parts]
    assert ok.status in ("available", "constrained"), ok.explanation
    evidence = ok.evidence
    assert evidence.inventory.pricing_rule_version == PRICING_RULE_VERSION
    assert evidence.lead_time.inventory_snapshot_id == evidence.inventory.snapshot_id is not None
    assert evidence.lead_time.capacity_snapshot_id == evidence.capacity.snapshot_id is not None
    assert ok.parts[0].stock.part_ids == ("P-100",) and ok.parts[0].stock.material_id == "MAT-001"
    if evidence.lead_time.result == "estimated":
        assert evidence.lead_time.estimated_ready_date >= HORIZON
    assert (revoked.status, revoked.evidence) == ("error", None)
