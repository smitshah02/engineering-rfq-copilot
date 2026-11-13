"""Day 37 opt-in live check: Intake validation's catalog path through the real MCP host.

``RFQ_AGENT_LIVE_MCP=1`` (marked ``integration``), same fixtures as the Day 34 live agent test:
a separate uvicorn MCP host on loopback TCP over the guarded, seeded test database. Pinned
manufacturing rules are read from that database. No model is involved.
"""

import os

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
from rfq_copilot.mcp.client import McpToolClient
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION, pinned_rules


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RFQ_AGENT_LIVE_MCP") != "1", reason="opt-in: RFQ_AGENT_LIVE_MCP=1")  # fmt: skip
@pytest.mark.asyncio(loop_scope="module")
async def test_intake_catalog_validation_through_the_live_mcp_host(trust, live, seed_database):
    from test_mcp_client import live_host

    async with seed_database.session() as s:
        rules = await pinned_rules(s, MANUFACTURING_RULE_VERSION)
    result = verify_extraction(proposal(complete_args()), COMPLETE, "live-src", "RFQ-LIVE")
    with live_host(trust, RFQ_MCP_HOST_RATE_LIMIT_PER_MINUTE="1000") as (base, secrets, log):
        integration = McpIntegration(McpToolClient(base, timeout_seconds=20), TestIssuer(trust), MCP_AUDIENCE, REST_AUDIENCE)  # fmt: skip

        def executor(name):
            principal = Principal("LIVE", frozenset(), user_id="LIVE", subject=live["subjects"][name])  # fmt: skip
            return g.mcp_executor(integration, principal, trust.mint(sub=live["subjects"][name]))

        ok = await validate_intake(result, COMPLETE, rules=rules, catalog=executor("a"))
        revoked = await validate_intake(result, COMPLETE, rules=rules, catalog=executor("revoked"))
    assert ok.readiness == "ready_for_manufacturing_evaluation", [p.findings for p in ok.parts]
    spec = ok.parts[0].specification
    assert (spec.material_id, spec.finish_id, spec.part_type) == ("MAT-001", "powder_coat", "flat_panel")  # fmt: skip
    assert ok.manufacturing_rule_version == MANUFACTURING_RULE_VERSION
    assert ok.manufacturing_rule_fingerprint == rules.fingerprint
    assert revoked.parts[0].material.status == "unavailable" and revoked.readiness == "requires_review"  # fmt: skip
    assert revoked.parts[0].specification is None
