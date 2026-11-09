"""Part and rule-version reads against the guarded, seeded, rolled-back test database."""

import hashlib
import json
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert, update
from test_database_queries import other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding import SOURCE
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session

from rfq_copilot.entities import Part
from rfq_copilot.models import BusinessRuleVersion
from rfq_copilot.repositories import RuleIntegrityError, get_rfq_parts, get_rule_version
from rfq_copilot.seeding.mapping import TABLES
from rfq_copilot.seeding.sources import RULE_SOURCES

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


def thaw(value):
    if hasattr(value, "keys"):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def stored_version(kind, path, version):
    digest = hashlib.sha256((SOURCE / path).read_bytes()).hexdigest()
    return version or f"{kind}@sha256:{digest}", digest


@pytest.mark.parametrize("kind,path,version", RULE_SOURCES)
async def test_every_rule_version_matches_its_source_file(populated, kind, path, version):
    rule_version, digest = stored_version(kind, path, version)
    rule = await get_rule_version(populated, kind, rule_version, expected_sha256=digest)
    assert (rule.rule_version, rule.rule_kind, rule.sha256) == (rule_version, kind, digest)
    # Exact numerics: the stored document equals the source parsed with Decimal floats.
    source = json.loads((SOURCE / path).read_text(), parse_float=Decimal)
    assert thaw(rule.document) == source
    again = await get_rule_version(populated, kind, rule_version)
    assert again.fingerprint == rule.fingerprint and len(rule.fingerprint) == 64


async def test_rule_numbers_are_decimal_and_document_is_read_only(populated):
    rule = await get_rule_version(populated, "manufacturing", "mfg-v1.0")
    limits = rule.document["limits"]
    assert (
        limits["min_tolerance_mm"] == Decimal("0.2") and type(limits["min_tolerance_mm"]) is Decimal
    )
    assert limits["thickness_mm"] == (Decimal("0.8"), Decimal("6.0"))
    with pytest.raises(TypeError):
        rule.document["version"] = "tampered"
    with pytest.raises(TypeError):
        limits["quantity"] = (0, 1)


async def test_unknown_version_is_none(populated):
    assert await get_rule_version(populated, "pricing", "price-v9.9") is None


async def test_wrong_kind_and_pin_mismatch_are_integrity_errors(populated):
    with pytest.raises(RuleIntegrityError, match="not manufacturing"):
        await get_rule_version(populated, "manufacturing", "price-v1.0")
    with pytest.raises(RuleIntegrityError, match="pinned"):
        await get_rule_version(populated, "pricing", "price-v1.0", expected_sha256="0" * 64)


async def test_document_claiming_another_version_is_rejected(populated):
    rule = await get_rule_version(populated, "pricing", "price-v1.0")
    await populated.execute(
        update(BusinessRuleVersion)
        .where(BusinessRuleVersion.rule_version == "price-v1.0")
        .values(document={**thaw(rule.document), "version": "price-v2.0"})
    )
    with pytest.raises(RuleIntegrityError, match="declares version price-v2.0"):
        await get_rule_version(populated, "pricing", "price-v1.0")


async def insert_runtime_rfq(session, customer):
    await session.execute(
        insert(TABLES["rfqs"]).values(
            rfq_id="TEST-DAY6-RFQ", customer_id=customer, source_type="mcp_shell"
        )
    )
    # Inserted out of order to prove line_number ordering.
    await session.execute(
        insert(TABLES["rfq_line_items"]),
        [
            {
                "rfq_line_id": uuid4(),
                "rfq_id": "TEST-DAY6-RFQ",
                "line_number": 2,
                "part_id": "PART-B",
                "quantity": 5,
                # executemany uses one key set; unstated fields must be explicit NULLs.
                **dict.fromkeys(
                    [
                        "part_type",
                        "material_id",
                        "finish_id",
                        "length_mm",
                        "width_mm",
                        "height_mm",
                        "thickness_mm",
                        "tolerance_mm",
                        "bend_radius_mm",
                        "hole_count",
                    ]
                ),
            },
            {
                "rfq_line_id": uuid4(),
                "rfq_id": "TEST-DAY6-RFQ",
                "line_number": 1,
                "part_id": "PART-A",
                "part_type": "l_bracket",
                "material_id": "MAT-009",
                "finish_id": "none",
                "length_mm": Decimal("450"),
                "width_mm": Decimal("490"),
                "height_mm": Decimal("170"),
                "thickness_mm": Decimal("1.0"),
                "tolerance_mm": Decimal("0.5"),
                "bend_radius_mm": Decimal("1.50"),
                "quantity": 85,
                "hole_count": 11,
            },
        ],
    )


async def test_rfq_parts_round_trip_in_line_order(populated, seed_plan):
    customer = seed_plan.rows["rfqs"][0]["customer_id"]
    await insert_runtime_rfq(populated, customer)
    parts = await get_rfq_parts(populated, "TEST-DAY6-RFQ", customer_id=customer)
    assert [p.part_id for p in parts] == ["PART-A", "PART-B"]
    assert parts[0] == Part.from_mapping(
        {
            "part_id": "PART-A",
            "part_type": "l_bracket",
            "material_id": "MAT-009",
            "finish_id": "none",
            "length_mm": 450,
            "width_mm": 490,
            "height_mm": 170,
            "thickness_mm": "1.0",
            "tolerance_mm": "0.5",
            "bend_radius_mm": "1.50",
            "quantity": 85,
            "hole_count": 11,
        }
    )
    assert parts[1] == Part(part_id="PART-B", quantity=5)
    assert (
        await get_rfq_parts(populated, "TEST-DAY6-RFQ", customer_id=other_customer(seed_plan))
        is None
    )


async def test_missing_and_historical_rfqs(populated, seed_plan):
    source = seed_plan.rows["rfqs"][0]
    assert await get_rfq_parts(populated, "MISSING", customer_id=source["customer_id"]) is None
    assert await get_rfq_parts(populated, source["rfq_id"], customer_id=source["customer_id"]) == ()


async def test_part_and_rule_reads_are_bounded_selects(populated, seed_database, seed_plan):
    customer = seed_plan.rows["rfqs"][0]["customer_id"]
    await insert_runtime_rfq(populated, customer)
    populated.expunge_all()
    with statements(seed_database) as captured:
        await get_rfq_parts(populated, "TEST-DAY6-RFQ", customer_id=customer)
    assert len(captured) == 2 and all(s.lstrip().upper().startswith("SELECT") for s in captured)
    with statements(seed_database) as captured:
        await get_rule_version(populated, "manufacturing", "mfg-v1.0")
    assert len(captured) == 1 and captured[0].lstrip().upper().startswith("SELECT")


async def test_hash_named_rule_must_match_its_stored_hash(populated):
    kind, path, _ = next(r for r in RULE_SOURCES if r[0] == "coordination")
    rule_version, _ = stored_version(kind, path, None)
    await populated.execute(
        update(BusinessRuleVersion)
        .where(BusinessRuleVersion.rule_version == rule_version)
        .values(sha256="f" * 64)
    )
    with pytest.raises(RuleIntegrityError):
        await get_rule_version(populated, kind, rule_version)
