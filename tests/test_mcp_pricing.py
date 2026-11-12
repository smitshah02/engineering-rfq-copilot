"""Day 26 Pricing MCP server against the real deterministic services (seeded test DB).

Registration, fail-closed rfq_id, forged-field and sanitizing checks for every planning
tool, pricing included, are parametrized in test_mcp_supply.py.
"""

import pytest
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day09_inventory import PANEL, pinned
from test_mcp_manufacturing import authenticated as authenticated
from test_mcp_manufacturing import text
from test_mcp_supply import MONDAY, PLAN, call, seeded

from rfq_copilot.entities import Part
from rfq_copilot.mcp import contracts as c
from rfq_copilot.mcp.pricing import create_pricing_server
from rfq_copilot.services.quotation import quote_parts

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

BRACKET = {**PANEL, "part_id": "PANEL-2", "quantity": 25}


async def test_costs_and_price_match_the_services_exactly(populated):
    entered = []
    server = create_pricing_server(seeded(populated, entered))
    costs = c.CostOutput.model_validate(
        (await call(server, "estimate_costs", PLAN)).structured_content
    )
    price = await call(server, "calculate_price", PLAN)
    assert not price.is_error, text(price)
    out = c.PriceOutput.model_validate(price.structured_content)
    direct = await quote_parts(
        populated, [Part.from_mapping(PANEL)], **await pinned(populated), horizon_start=MONDAY
    )
    assert len(entered) == 2  # one snapshot per tool call; prerequisites recomputed inside
    assert (
        costs.result == "costed"
        and costs.total_manufacturing_cost == direct.costs.total_manufacturing_cost
    )
    assert out.result == "priced" and out.quote_total_usd == direct.quote_total_usd
    assert out.calculation_id == direct.calculation_id
    assert out.lines[0].quoted_unit_price_usd == direct.pricing.lines[0].quoted_unit_price_usd
    wire = price.structured_content
    assert wire["quote_total_usd"] == str(direct.quote_total_usd)  # exact string, scale kept
    assert not out.tax_included and out.pricing_rule_version == "price-v1.0"


async def test_multi_part_quotation_matches_quote_parts(populated):
    request = {**PLAN, "parts": [PANEL, BRACKET], "requested_delivery_date": "2027-01-29"}
    result = await call(create_pricing_server(seeded(populated, [])), "evaluate_quotation", request)
    assert not result.is_error, text(result)
    out = c.QuotationOutput.model_validate(result.structured_content)
    direct = await quote_parts(
        populated,
        [Part.from_mapping(PANEL), Part.from_mapping(BRACKET)],
        **await pinned(populated),
        horizon_start=MONDAY,
        requested_delivery_date=MONDAY.replace(year=2027, month=1, day=29),
    )
    assert out.result == direct.result == "quoted"
    assert [ln.part_id for ln in out.lines] == ["PANEL", "PANEL-2"]
    assert [ln.line_total_usd for ln in out.lines] == [
        ln.priced.line_total_usd for ln in direct.lines
    ]
    assert (out.quote_total_usd, out.estimated_ready_date, out.calculation_id) == (
        direct.quote_total_usd,
        direct.estimated_ready_date,
        direct.calculation_id,
    )
    assert set(result.structured_content) == set(
        c.QuotationOutput.model_fields
    )  # no approval/release


async def test_unmanufacturable_quotation_is_a_result_not_an_error(populated):
    request = {**PLAN, "parts": [{**PANEL, "material_id": "NOPE-404"}]}
    for name in ("estimate_costs", "calculate_price", "evaluate_quotation"):
        result = await call(create_pricing_server(seeded(populated, [])), name, request)
        assert not result.is_error, (name, text(result))
        assert result.structured_content["result"] == "not_manufacturable"
