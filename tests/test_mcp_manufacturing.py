"""Day 25 Manufacturing MCP server: registration, schemas, error safety, seeded behaviour."""

import json
from contextlib import asynccontextmanager, contextmanager

import pytest
from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day08_manufacturing import base_parts

from rfq_copilot.database import DatabaseError
from rfq_copilot.mcp import contracts as c
from rfq_copilot.mcp import manufacturing as m
from rfq_copilot.mcp import runtime
from rfq_copilot.mcp.security import McpPrincipal
from rfq_copilot.repositories.queries import RuleIntegrityError
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES

SECRET = "sk-FAKE-SECRET-7f3a9"
NAMES = [
    "search_materials",
    "get_material",
    "check_material_suitability",
    "check_manufacturing_feasibility",
]
PART = {"part_id": "P-1", "material_id": "AL5052", "thickness_mm": "2.0"}
VALID_ARGS = {
    "search_materials": {"request": {}},
    "get_material": {"request": {"material_id": "AL5052"}},
    "check_material_suitability": {"request": {"part": PART}},
    "check_manufacturing_feasibility": {"request": {"part": PART}},
}
ALLOWED = {f"{k.value}: {v}" for k, v in c.SAFE_MESSAGES.items()}


ALL_PERMISSIONS = frozenset(t.permission for t in c.TOOLS)


@contextmanager
def acting_as(permissions=ALL_PERMISSIONS, customer="CUST-TEST", user="U-TEST", assigned=False):
    """Test harness: the request-scoped SDK auth context an HTTP bearer request would carry.

    The real bearer path (token -> McpTokenVerifier -> context) is tested in
    test_mcp_auth.py; here the principal object is built directly so the authorization
    wrapper and the tools run without a database identity.
    """
    principal = None
    if permissions is not None:
        principal = McpPrincipal(
            token="test-only",
            client_id=user,
            scopes=sorted(permissions),
            user_id=user,
            customer_id=customer,
            permissions=frozenset(permissions),
            assignment_required=assigned,
        )
    token = auth_context_var.set(None if principal is None else AuthenticatedUser(principal))
    try:
        yield principal
    finally:
        auth_context_var.reset(token)


@pytest.fixture(autouse=True)
def authenticated():
    """Day 27: every tool needs a verified principal; these tests act as a fully granted one."""
    with acting_as():
        yield


@asynccontextmanager
async def no_db():
    yield object()  # unit tests never touch it: services are replaced or never reached


def text(result):
    return " ".join(block.text for block in result.content)


def server(snapshot=no_db):
    return m.create_manufacturing_server(snapshot)


# --- registration and schemas (no database) ------------------------------------------


@pytest.mark.asyncio
async def test_exactly_the_four_manufacturing_tools_with_contract_schemas():
    async with Client(server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert sorted(tools) == sorted(NAMES)
    contracts = {t.name: t for t in c.TOOLS}
    for name, tool in tools.items():
        contract = contracts[name]
        assert tool.description == contract.description
        assert tool.annotations.read_only_hint is True and not tool.annotations.destructive_hint
        assert tool.input_schema["properties"]["request"]["$ref"].endswith(
            contract.input_model.__name__
        )
        assert tool.output_schema["title"] == contract.output_model.__name__
        assert set(tool.output_schema["properties"]) == set(contract.output_model.model_fields)


# --- sanitizing and pre-handler rejection (wire path, no database) --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: {"request": {**a["request"], "customer_id": SECRET}},
        lambda a: {"request": {**a["request"], "role": SECRET}},
        lambda a: {"request": {**a["request"], "permissions": [SECRET]}},
        lambda a: {"request": {**a["request"], "rule_version": SECRET}},
        lambda a: {"request": {**a["request"], SECRET: 1}},  # secret as a key
        lambda a: {"request": SECRET},
        lambda a: {**a, "approval_rules": SECRET},
    ],
    ids=["customer", "role", "permissions", "rule", "key", "payload", "approval"],
)
async def test_invalid_input_is_rejected_before_services_and_never_echoed(
    monkeypatch, name, mutate
):
    def forbidden(*args, **kwargs):
        raise AssertionError("a business service ran on invalid input")

    for attr in (
        "material_page",
        "get_material_spec",
        "check_part_material",
        "check_manufacturing_capability",
    ):
        monkeypatch.setattr(m, attr, forbidden)
    monkeypatch.setattr(runtime, "get_rule_version", forbidden)
    async with Client(server()) as client:
        result = await client.call_tool(name, mutate(VALID_ARGS[name]))
    assert result.is_error and text(result) == "INVALID_INPUT: Invalid tool arguments."
    assert SECRET not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (RuleIntegrityError(f"Rule {SECRET} differs"), "SNAPSHOT_CONFLICT"),
        (DatabaseError(f"host=db password={SECRET}"), "INTERNAL"),
        (RuntimeError(f"SELECT * FROM business.users -- {SECRET}"), "INTERNAL"),
        # Day 26 A1: a service ValueError after contract validation is a defect, not input.
        (ValueError(f"part.thickness_mm {SECRET}"), "INTERNAL"),
    ],
)
async def test_internal_failures_become_fixed_safe_codes(monkeypatch, failure, code):
    async def boom(*args, **kwargs):
        raise failure

    monkeypatch.setattr(m, "get_material_spec", boom)
    async with Client(server()) as client:
        result = await client.call_tool("get_material", VALID_ARGS["get_material"])
    assert result.is_error and text(result) == f"{code}: {c.SAFE_MESSAGES[c.ErrorCode(code)]}"
    assert SECRET not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("seeded", [None, RuleIntegrityError("tampered")])
async def test_unseeded_or_tampered_rules_are_a_snapshot_conflict(monkeypatch, seeded):
    async def rules(session, kind, version, *, expected_sha256):
        assert (kind, version) == ("manufacturing", runtime.MANUFACTURING_RULE_VERSION)
        assert expected_sha256 == TRUSTED_RULES[version][1]  # pinned, server-chosen
        if seeded is not None:
            raise seeded
        return None

    monkeypatch.setattr(runtime, "get_rule_version", rules)
    async with Client(server()) as client:
        result = await client.call_tool(
            "check_manufacturing_feasibility", VALID_ARGS["check_manufacturing_feasibility"]
        )
    assert text(result) == "SNAPSHOT_CONFLICT: " + c.SAFE_MESSAGES[c.ErrorCode.SNAPSHOT_CONFLICT]


def test_sanitizer_allow_list():
    from rfq_copilot.mcp.safety import _safe_text

    assert (
        _safe_text("Error executing tool x: NOT_FOUND: Resource not found or not visible.")
        == "NOT_FOUND: Resource not found or not visible."
    )
    assert (
        _safe_text(f"Error executing tool x: 1 validation error input_value='{SECRET}'") in ALLOWED
    )
    assert _safe_text("Error executing tool x") == "INTERNAL: Internal error."
    assert SECRET not in _safe_text(f"Error executing tool x: INTERNAL: Internal error. {SECRET}")


# --- seeded test database (guarded, rolled back) ---------------------------------------


def wire(part):
    """Ground-truth JSON part as tool arguments: no nulls, exact decimals as strings."""
    return {k: str(v) if isinstance(v, float) else v for k, v in part.items() if v is not None}


def seeded(session, entered):
    @asynccontextmanager
    async def snapshot():
        entered.append(session)
        yield session

    return server(snapshot)


async def call(srv, name, request):
    async with Client(srv) as client:
        return await client.call_tool(name, {"request": request})


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_seeded_tools_return_contract_outputs_in_one_snapshot(populated, name):
    entered = []
    part = next(iter(base_parts().values()))
    request = {
        "search_materials": {"limit": 2},
        "get_material": {"material_id": part["material_id"]},
    }.get(name, {"part": wire(part)})
    result = await call(seeded(populated, entered), name, request)
    assert not result.is_error, text(result)
    assert len(entered) == 1  # one consistent snapshot per call
    out = {t.name: t.output_model for t in c.TOOLS}[name].model_validate(result.structured_content)
    encoded = json.dumps(result.structured_content)
    assert "e+" not in encoded.lower()
    if name == "search_materials":
        assert len(out.items) == 2 and out.has_more
    elif name == "get_material":
        assert all(isinstance(v, str) for v in result.structured_content["supported_thickness_mm"])
    else:
        assert out.result == "valid" and out.rule_version == runtime.MANUFACTURING_RULE_VERSION
    if name == "check_manufacturing_feasibility":
        assert out.operations and out.material.result == "valid"


@pytest.mark.parametrize(
    ("change", "expected", "finding"),
    [
        ({"material_id": "NOPE-404"}, "unsupported", "UNSUPPORTED_MATERIAL"),
        ({"thickness_mm": None}, "needs_clarification", "MISSING_THICKNESS_MM"),
    ],
)
@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_seeded_business_rejections_are_results_not_errors(
    populated, change, expected, finding
):
    part = {**next(iter(base_parts().values())), **change}
    part = wire(part)
    for name in ("check_material_suitability", "check_manufacturing_feasibility"):
        result = await call(seeded(populated, []), name, {"part": part})
        assert not result.is_error
        assert result.structured_content["result"] == expected
        assert finding in result.structured_content["findings"]


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_seeded_missing_material_is_not_found(populated):
    result = await call(seeded(populated, []), "get_material", {"material_id": "NOPE-404"})
    assert result.is_error and text(result) == "NOT_FOUND: Resource not found or not visible."
