"""Day 41 opt-in live check: A1 -> A2 -> A3 -> A4 -> A5 through the real MCP host and seeded
services.

``RFQ_AGENT_LIVE_MCP=1`` (marked ``integration``), the Day 39/40 fixtures: a separate uvicorn MCP
host on loopback TCP over the guarded, seeded test database. No model. The pricing and approval
rules are the ones ``pinned_rules`` loads and verifies against the manifest; ``approval-v1.2``
(post-baseline, TD-004) is registered on the test database first with the existing idempotent
``register_rule_version``. A5 writes nothing.
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
from rfq_copilot.mcp.contracts import NEW_EVALUATION_APPROVAL_RULE_VERSION
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION, PRICING_RULE_VERSION, pinned_rules
from rfq_copilot.pricing_agent import evaluate_pricing
from rfq_copilot.quotation_agent import evaluate_quotation_review
from rfq_copilot.seeding.rules import register_rule_version
from rfq_copilot.supply_agent import evaluate_supply

HORIZON = date(2026, 11, 9)


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RFQ_AGENT_LIVE_MCP") != "1", reason="opt-in: RFQ_AGENT_LIVE_MCP=1")  # fmt: skip
@pytest.mark.asyncio(loop_scope="module")
async def test_quotation_review_agent_through_the_live_mcp_host(trust, live, seed_database):
    from test_mcp_client import live_host

    async with seed_database.session() as s, s.begin():
        await register_rule_version(s, NEW_EVALUATION_APPROVAL_RULE_VERSION)
    async with seed_database.session() as s:
        rules = await pinned_rules(s, MANUFACTURING_RULE_VERSION)
        pricing = await pinned_rules(s, PRICING_RULE_VERSION)
        approval = await pinned_rules(s, NEW_EVALUATION_APPROVAL_RULE_VERSION)
    policy = {"pricing_rules": pricing, "approval_rules": approval}
    extraction = verify_extraction(proposal(complete_args()), COMPLETE, "live-src", "RFQ-LIVE")
    with live_host(trust, RFQ_MCP_HOST_RATE_LIMIT_PER_MINUTE="1000") as (base, secrets, log):
        integration = McpIntegration(McpToolClient(base, timeout_seconds=20), TestIssuer(trust), MCP_AUDIENCE, REST_AUDIENCE)  # fmt: skip

        def executor(name):
            principal = Principal("LIVE", frozenset(), user_id="LIVE", subject=live["subjects"][name])  # fmt: skip
            return g.mcp_executor(integration, principal, trust.mint(sub=live["subjects"][name]))

        intake = await validate_intake(extraction, COMPLETE, rules=rules, catalog=executor("a"))
        a2 = await evaluate_manufacturing_feasibility(intake, executor("a"))
        a3 = await evaluate_supply(a2, executor("a"), horizon_start=HORIZON, extraction=extraction)
        a4 = await evaluate_pricing(a2, a3, executor("a"), pricing_rules=pricing)
        ok = await evaluate_quotation_review(a2, a3, a4, executor("a"), **policy)
        # manufacturing_engineer: no view_quote in rbac-policy-v2, so no draft and no amount.
        restricted = await evaluate_quotation_review(a2, a3, a4, executor("restricted"), **policy)
        revoked = await evaluate_quotation_review(a2, a3, a4, executor("revoked"), **policy)
    assert a4.status == "priced", a4.explanation
    assert ok.status in ("draft_ready_for_review", "requires_review"), ok.explanation
    assert ok.status == ("requires_review" if a3.status == "constrained" else ok.status)
    assert (ok.approved, ok.released, ok.required_approver_role) == (False, False, "sales_manager")
    assert (ok.approval_rule_version, ok.approval_rule_fingerprint) == (approval.rule_version, approval.fingerprint)  # fmt: skip
    assert (ok.quote_total_usd, ok.calculation_id, ok.currency) == (a4.quote_total_usd, a4.calculation_id, "USD")  # fmt: skip
    assert [(ln.part_id, ln.line_total_usd) for ln in ok.lines] == [(ln.part_id, ln.line_total_usd) for ln in a4.lines]  # fmt: skip
    assert ok.estimated_ready_date == a3.evidence.lead_time.estimated_ready_date
    assert (restricted.status, restricted.error, restricted.lines, restricted.evidence) == ("error", "forbidden", (), None)  # fmt: skip
    assert (revoked.status, revoked.lines, revoked.evidence) == ("error", (), None)
