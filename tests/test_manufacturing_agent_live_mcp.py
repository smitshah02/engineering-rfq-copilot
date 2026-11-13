"""Day 38 opt-in live check: A2 through the real MCP host and seeded manufacturing services.

``RFQ_AGENT_LIVE_MCP=1`` (marked ``integration``), same fixtures as the Day 37 live Intake test:
a separate uvicorn MCP host on loopback TCP over the guarded, seeded test database. No model.
"""

import os
from decimal import Decimal

import pytest
from test_auth import AUDIENCE as REST_AUDIENCE
from test_auth import trust as trust
from test_intake import proposal
from test_intake_validation import COMPLETE, complete_args
from test_manufacturing_agent import ready, spec
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
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION, pinned_rules


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RFQ_AGENT_LIVE_MCP") != "1", reason="opt-in: RFQ_AGENT_LIVE_MCP=1")  # fmt: skip
@pytest.mark.asyncio(loop_scope="module")
async def test_manufacturing_agent_through_the_live_mcp_host(trust, live, seed_database):
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
        ok = await evaluate_manufacturing_feasibility(intake, executor("a"))
        holes = {
            n: await evaluate_manufacturing_feasibility(
                ready(spec(hole_count=n), fingerprint=rules.fingerprint), executor("a")
            )
            for n in (300, 301)
        }
        thin = await evaluate_manufacturing_feasibility(
            ready(spec(thickness_mm=Decimal("2.5")), fingerprint=rules.fingerprint), executor("a")
        )
        revoked = await evaluate_manufacturing_feasibility(intake, executor("revoked"))
    assert intake.readiness == "ready_for_manufacturing_evaluation"
    assert ok.status == "feasible", [p.explanation for p in ok.parts]
    out = ok.parts[0].evidence.output
    assert (out.rule_version, out.rule_fingerprint) == (MANUFACTURING_RULE_VERSION, rules.fingerprint)  # fmt: skip
    assert out.operations[0] == "laser" and "finish" in out.operations and out.operations[-1] == "pack"  # fmt: skip
    assert (holes[300].status, holes[301].status) == ("feasible", "infeasible")  # MFG-001
    assert "OUT_OF_RANGE_HOLE_COUNT" in holes[301].parts[0].evidence.output.findings
    assert thin.status == "infeasible"
    assert revoked.status == "error" and revoked.parts[0].evidence is None
