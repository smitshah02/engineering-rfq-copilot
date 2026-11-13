"""Day 43 opt-in live check: the integrated A1 -> A5 workflow through the real MCP host and the
seeded services.

``RFQ_AGENT_LIVE_MCP=1`` (marked ``integration``), the Day 39-41 fixtures: a separate uvicorn MCP
host on loopback TCP over the guarded, seeded test database. A1 runs its real graph on the
Day 36 fake chat model (no paid or local model). Rules are the ones ``pinned_rules`` loads and
verifies; ``approval-v1.2`` (post-baseline, TD-004) is registered on the test database first
with the existing idempotent ``register_rule_version``, exactly as the Day 41 live test does.
The workflow itself writes nothing.
"""

import os
from datetime import date

import pytest
from test_auth import AUDIENCE as REST_AUDIENCE
from test_auth import trust as trust
from test_intake import model, reply
from test_intake_validation import COMPLETE, complete_args
from test_mcp_auth import MCP_AUDIENCE
from test_mcp_client import TestIssuer
from test_mcp_live_e2e import live as live
from test_mcp_live_e2e import seed_bundle as seed_bundle
from test_mcp_live_e2e import seed_database as seed_database
from test_mcp_live_e2e import seed_plan as seed_plan
from test_mcp_live_e2e import session as session

from rfq_copilot import graph as g
from rfq_copilot import intake
from rfq_copilot.agent_integration import evaluate_rfq_workflow
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.mcp_integration import McpIntegration
from rfq_copilot.mcp.client import McpToolClient
from rfq_copilot.mcp.contracts import NEW_EVALUATION_APPROVAL_RULE_VERSION
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION, PRICING_RULE_VERSION, pinned_rules
from rfq_copilot.seeding.rules import register_rule_version

HORIZON = date(2026, 11, 9)


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RFQ_AGENT_LIVE_MCP") != "1", reason="opt-in: RFQ_AGENT_LIVE_MCP=1")  # fmt: skip
@pytest.mark.asyncio(loop_scope="module")
async def test_integrated_workflow_through_the_live_mcp_host(trust, live, seed_database):
    from test_mcp_client import live_host

    async with seed_database.session() as s, s.begin():
        await register_rule_version(s, NEW_EVALUATION_APPROVAL_RULE_VERSION)
    async with seed_database.session() as s:
        rules = {
            "manufacturing_rules": await pinned_rules(s, MANUFACTURING_RULE_VERSION),
            "pricing_rules": await pinned_rules(s, PRICING_RULE_VERSION),
            "approval_rules": await pinned_rules(s, NEW_EVALUATION_APPROVAL_RULE_VERSION),
        }
    with live_host(trust, RFQ_MCP_HOST_RATE_LIMIT_PER_MINUTE="1000") as (base, secrets, log):
        integration = McpIntegration(McpToolClient(base, timeout_seconds=20), TestIssuer(trust), MCP_AUDIENCE, REST_AUDIENCE)  # fmt: skip

        async def workflow(name):
            principal = Principal("LIVE", frozenset(), user_id="LIVE", subject=live["subjects"][name])  # fmt: skip
            execute = g.mcp_executor(integration, principal, trust.mint(sub=live["subjects"][name]))
            graph = intake.build_intake_graph(model(reply(complete_args())))
            return await evaluate_rfq_workflow(COMPLETE, source_id="live-src", rfq_id="RFQ-LIVE", intake_graph=graph, execute=execute, horizon_start=HORIZON, **rules)  # fmt: skip

        ok = await workflow("a")
        # manufacturing_engineer: no view_quote in rbac-policy-v2, so no price and no draft.
        restricted = await workflow("restricted")
        revoked = await workflow("revoked")

    assert ok.stopped_at is None and ok.status in ("draft_ready_for_review", "requires_review"), ok.summary()  # fmt: skip
    assert [s.agent for s in ok.stages] == ["A1", "A1", "A2", "A3", "A4", "A5"]
    assert ok.status == ("requires_review" if ok.supply.status == "constrained" else ok.status)
    assert ok.rules == tuple((r.rule_version, r.fingerprint) for r in rules.values())
    q, p, lead = ok.quotation, ok.pricing, ok.supply.evidence.lead_time
    assert (q.quote_total_usd, q.calculation_id, q.currency) == (p.quote_total_usd, p.calculation_id, "USD")  # fmt: skip
    assert [(ln.part_id, ln.line_total_usd) for ln in q.lines] == [(ln.part_id, ln.line_total_usd) for ln in p.lines]  # fmt: skip
    assert q.estimated_ready_date == lead.estimated_ready_date
    assert (ok.approved, ok.released, q.approved, q.released) == (False, False, False, False)
    assert ok.customer_view() is not None and ok.customer_view().conditional is True
    for denied in (restricted, revoked):
        assert denied.status != "draft_ready_for_review" and denied.stopped_at is not None, denied.summary()  # fmt: skip
        assert (denied.draft_available, denied.customer_view()) == (False, None)
        assert denied.pricing is None or (denied.pricing.lines, denied.pricing.evidence) == ((), None)  # fmt: skip
        assert denied.quotation is None or denied.quotation.lines == ()
    # Wherever the server denies the role, the workflow stops there (A1 turns a denied catalog
    # lookup into a review finding; later agents report the server's ``forbidden``).
    if restricted.stopped_at != "intake_validation":
        assert restricted.error == "forbidden", restricted.summary()
