"""Structured portal intake regressions; no database, Ollama or live MCP.

Run the actual Intake and Supervisor graphs and deterministic service fakes. Only their
external boundaries are mocked. The incident's original Ollama responses are unavailable;
faulty responses below characterize the failure without claiming to recover those responses.
"""

import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from test_hardening_services import APPROVAL_V12
from test_intake import ev, model, reply
from test_intake_validation import COMPLETE, RULES, Catalog, complete_args
from test_pricing_agent import TRUSTED
from test_specialized_agent_integration import Router
from test_supervisor_delegation import ALICE, STAGES, Spy
from test_supply_agent import MONDAY

from rfq_copilot import intake, llm, quote_drafts, repositories
from rfq_copilot import intake_validation as a1v
from rfq_copilot import supervisor as sv
from rfq_copilot import workflow_worker as ww
from rfq_copilot.api.schemas.rfq import PartSpecification
from rfq_copilot.mcp import runtime
from rfq_copilot.mcp.client import McpCallError, McpToolClient
from rfq_copilot.mcp.contracts import PartCheckInput, PlanningInput

pytestmark = pytest.mark.asyncio
PART_ID = "P-TEST-BRACKET-002"
BRACKET = dict(
    part_id=PART_ID,
    part_type="l_bracket",
    material_id="MAT-001",
    finish_id="none",
    length_mm=Decimal("100.000"),
    width_mm=Decimal("60.000"),
    height_mm=Decimal("30.000"),
    thickness_mm=Decimal("2.000"),
    bend_radius_mm=Decimal("4.000"),
    tolerance_mm=Decimal("0.200"),
    quantity=20,
    hole_count=0,
)
RULE_SET = {"manufacturing": RULES, "pricing": TRUSTED, "approval": APPROVAL_V12}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("use injected offline dependencies")

    monkeypatch.setattr(McpToolClient, "call", forbidden)
    original = sv._manufacturing_trusted
    monkeypatch.setattr(sv, "_manufacturing_trusted", lambda r: r is RULES or original(r))


def specification(**over):
    return PartSpecification(**(BRACKET | over))


def structured(*specs, **over):
    return intake.StructuredIntakeSource(
        **{
            "specifications": specs or (specification(),),
            "material_names": (("MAT-001", "aluminum 5052-H32"),),
            "finish_names": (("none", "unfinished"),),
            **over,
        }
    )


async def extract(source, graph=None):
    return await intake.run_intake(
        graph or intake.build_intake_graph(model(reply({"parts": []}))),
        source.render()[0],
        source_id="structured-source",
        rfq_id="RFQ-new",
        structured_source=source,
    )


async def validate(source, extraction=None, **over):
    extraction = extraction or await extract(source)
    return await a1v.validate_intake(
        extraction,
        source.render()[0],
        structured_source=source,
        **{"rules": RULES, "catalog": Catalog(), **over},
    )


def context(source, router=None, **over):
    router = router or Router()
    lines = [SimpleNamespace(**s.model_dump()) for s in source.specifications]
    return sv.SupervisorContext(
        **{
            "principal": ALICE,
            "execute": ww.SourceGate(router, lines),
            "rfq_id": "RFQ-new",
            "source_id": "structured-source",
            "source_text": source.render()[0],
            "structured_source": source,
            "trusted_part_ids": tuple(s.part_id for s in source.specifications),
            "manufacturing_rules": RULES,
            "pricing_rules": TRUSTED,
            "approval_rules": APPROVAL_V12,
            "horizon_start": MONDAY,
            "intake_graph": intake.build_intake_graph(model(reply({"parts": []}))),
            **over,
        }
    )


async def test_exact_persisted_bracket_is_one_complete_authoritative_a1_part():
    source = structured()
    extracted = await extract(source)
    checked = await validate(source, extracted)
    assert extracted.status == "extracted" and extracted.readiness == "pending_validation"
    assert extracted.part_boundaries == "single_part"
    assert len(extracted.parts) == len(checked.parts) == 1
    assert extracted.discarded_empty_entries == 0
    assert checked.readiness == "ready_for_manufacturing_evaluation"
    assert checked.clarifications == () and checked.parts[0].findings == ()
    assert checked.parts[0].specification == specification()
    assert checked.parts[0].specification.bend_radius_mm == Decimal("4.000")
    assert not checked.feasibility_evaluated


@pytest.mark.parametrize(
    "bad_parts",
    [
        [{"label": PART_ID}, {"label": PART_ID}],  # duplicate and incomplete
        [{"quantity": ev(999, "invented quantity 999")}],  # incomplete and conflicting
        [{"label": "P-INVENTED-999"}, {"label": PART_ID}],
        [],
    ],
)
async def test_structured_input_does_not_sample_a_lossy_or_invented_model_proposal(
    monkeypatch, bad_parts
):
    invocation = AsyncMock(
        return_value=intake.ExtractionProposal.model_validate({"parts": bad_parts})
    )
    monkeypatch.setattr(llm, "invoke_structured", invocation)
    source = structured()
    extracted = await extract(source)
    checked = await validate(source, extracted)
    invocation.assert_not_awaited()
    assert [p.label for p in extracted.parts] == [PART_ID]
    assert checked.parts[0].specification == source.specifications[0]


@pytest.mark.parametrize(
    "field,question",
    [
        ("part_type", "part_type"),
        ("material_id", "material_id"),
        ("finish_id", "finish_id"),
        ("length_mm", "length"),
        ("width_mm", "width"),
        ("height_mm", "height"),
        ("thickness_mm", "thickness"),
        ("tolerance_mm", "tolerance"),
        ("quantity", "quantity"),
    ],
)
async def test_genuine_missing_structured_fields_stop_before_a2(field, question):
    source = structured(specification(**{field: None}))
    router = Router()
    checked = await validate(source)
    assert checked.readiness == "needs_clarification"
    assert checked.parts[0].specification is None
    assert any(c.field == question and c.kind == "missing" for c in checked.clarifications)
    result = await sv.run_supervisor_workflow(context(source, router))
    assert result.route.stopped_at == "intake_validation"
    assert "check_manufacturing_feasibility" not in router.calls
    assert not result.draft_available


async def test_two_distinct_parts_retain_their_own_fields_order_and_evidence():
    first = specification()
    second = specification(
        part_id="P-TEST-BRACKET-003",
        quantity=41,
        width_mm="100.000",
        height_mm="40.000",
        bend_radius_mm="5.000",
    )
    source = structured(first, second)
    extracted = await extract(source)
    checked = await validate(source, extracted)
    assert extracted.part_boundaries == "labelled"
    assert [p.index for p in extracted.parts] == [1, 2]
    assert checked.readiness == "ready_for_manufacturing_evaluation"
    assert tuple(p.specification for p in checked.parts) == (first, second)
    # Equal length and width are legitimate structured fields, never shared-value guesses.
    assert second.length_mm == second.width_mm
    for part in extracted.parts:
        assert all(part.scope[0] <= f.span[0] < f.span[1] <= part.scope[1] for f in part.fields)


@pytest.mark.parametrize("second", [BRACKET, BRACKET | {"width_mm": Decimal("65.000")}])
async def test_duplicate_owners_and_contradictory_authoritative_rows_are_rejected(second):
    with pytest.raises(ValidationError, match="distinct owners"):
        structured(specification(), PartSpecification(**second))


async def test_conflicting_trusted_owner_list_is_refused_before_catalog():
    source = structured()
    catalog = Catalog()
    with pytest.raises(ValueError, match="owners"):
        await validate(source, catalog=catalog, trusted_part_ids=("P-OTHER",))
    assert catalog.calls == []


@pytest.mark.parametrize(
    "change", ["duplicate", "invented", "missing", "ownership", "value", "span"]
)
async def test_structured_validation_refuses_any_modified_a1_candidate_or_provenance(change):
    source = structured()
    original = await extract(source)
    (part,) = original.parts
    if change == "duplicate":
        parts = (part, part.model_copy(update={"index": 2}))
    elif change == "invented":
        parts = (part, part.model_copy(update={"index": 2, "label": "P-INVENTED"}))
    elif change == "missing":
        parts = ()
    elif change == "ownership":
        parts = (part.model_copy(update={"label": "P-OTHER"}),)
    else:
        field = part.fields[0]
        update = {"normalized": "P-OTHER"} if change == "value" else {"span": (0, 1)}
        parts = (
            part.model_copy(update={"fields": (field.model_copy(update=update), *part.fields[1:])}),
        )
    catalog = Catalog()
    with pytest.raises(ValueError, match="extraction"):
        await validate(source, original.model_copy(update={"parts": parts}), catalog=catalog)
    assert catalog.calls == []


async def test_exact_source_provenance_includes_ids_decimal_scale_and_original_offsets():
    source = structured()
    text = source.render()[0]
    extracted = await extract(source)
    assert extracted.source_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert extracted.source_id == "structured-source" and extracted.rfq_id == "RFQ-new"
    assert "material_id: MAT-001\n" in text and "finish_id: none\n" in text
    assert "length_mm: 100.000\n" in text and "tolerance_mm: 0.200\n" in text
    for part in extracted.parts:
        for field in part.fields:
            a, b = field.span
            assert text[a:b] == field.evidence
            assert field.status == "verified"
            assert field.transformation.startswith("persisted_specification:")
    with pytest.raises(ValueError, match="source"):
        await a1v.validate_intake(extracted, text + " ", rules=RULES, structured_source=source)
    graph = intake.build_intake_graph(model(reply({"parts": []})))
    invalid = await intake.run_intake(
        graph, text + "Notes: Width 65 mm.", source_id="structured-source", structured_source=source
    )
    assert invalid.status == "error" and invalid.error == "invalid_input"


@pytest.mark.parametrize("names", [(), (("MAT-001", "aluminum 6061-T6"),)])
async def test_missing_or_inconsistent_catalog_wording_cannot_replace_an_authoritative_id(names):
    source = structured(material_names=names)
    checked = await validate(source)
    assert checked.readiness == "requires_review"
    assert checked.parts[0].specification is None
    assert source.specifications[0].material_id == "MAT-001"


@pytest.mark.parametrize(
    "catalog", [None, Catalog(fail=McpCallError("unavailable")), Catalog(raw={})]
)
async def test_structured_catalog_failure_is_never_a_ready_specification(catalog):
    checked = await validate(structured(), catalog=catalog)
    assert checked.readiness == "requires_review" and checked.parts[0].specification is None


async def test_structured_catalog_timeout_cannot_leave_a_ready_specification():
    checked = await validate(structured(), catalog=Catalog(delay=1), timeout_seconds=0.001)
    assert checked.readiness == "requires_review"
    assert all(p.specification is None for p in checked.parts)
    assert any(f.kind == "catalog_unavailable" for f in checked.findings)


async def test_structured_required_fields_still_come_from_the_pinned_rules():
    checked = await validate(structured(), rules=None)
    assert checked.readiness == "requires_review"
    assert any(f.kind == "rules_not_checked" for f in checked.parts[0].findings)
    changed = replace(
        RULES,
        document={
            **RULES.document,
            "mandatory_fields": [*RULES.document["mandatory_fields"], "bend_radius_mm"],
        },
    )
    checked = await validate(structured(specification(bend_radius_mm=None)), rules=changed)
    assert checked.readiness == "needs_clarification"
    assert any(c.field == "bend_radius_mm" for c in checked.clarifications)
    assembly = structured(specification(part_type="fabricated_assembly", weld_length_mm="120.000"))
    checked = await validate(assembly)
    assert checked.parts[0].specification == assembly.specifications[0]
    checked = await validate(structured(specification(part_type="fabricated_assembly")))
    assert any(c.field == "weld_length_mm" for c in checked.clarifications)


@pytest.mark.parametrize("part_count", [1, 2])
async def test_structured_supervisor_runs_real_a1_to_a5_with_unchanged_manufacturing_handoff(
    monkeypatch, part_count
):
    specs = (specification(), specification(part_id="P-TEST-BRACKET-003", quantity=41))
    source = structured(*specs[:part_count])
    invocation = AsyncMock(side_effect=AssertionError("structured input must not call a model"))
    monkeypatch.setattr(llm, "invoke_structured", invocation)
    spy = Spy(monkeypatch)
    router = Router()
    ctx = context(source, router)
    result = await sv.run_supervisor_workflow(ctx)
    assert result.status == "executed"
    assert spy.stages() == STAGES
    assert [o.stage for o in result.outcomes] == STAGES
    validation = spy.one("intake_validation")
    assert validation[1][0] is spy.one("intake_extraction")[3]
    assert validation[1][1] is ctx.source_text
    assert spy.one("manufacturing")[1][0] is validation[3]
    assert tuple(p.specification for p in validation[3].parts) == source.specifications
    manufacture = spy.one("manufacturing")[3]
    assert tuple(p.specification for p in manufacture.parts) == source.specifications
    for tool, request in router.requests:
        if isinstance(request, PartCheckInput):
            assert request.part in source.specifications
        elif isinstance(request, PlanningInput):
            assert request.parts == source.specifications
    assert manufacture.parts[0].evidence is not None
    assert "check_inventory" in router.calls and "check_capacity" in router.calls
    assert "estimate_costs" in router.calls and "evaluate_quotation" in router.calls
    assert not ctx.execute.mismatch
    assert result.human_approval_required and not result.approved and not result.released
    invocation.assert_not_awaited()


@pytest.mark.parametrize(
    "over",
    [
        {"tolerance_mm": "0.100"},
        {"bend_radius_mm": "1.000"},
        {"width_mm": "1001.000"},
        {"quantity": 1001},
    ],
)
async def test_worker_cannot_create_a_quote_when_deterministic_manufacturing_fails(
    monkeypatch, over
):
    source = structured(specification(**over))
    lines = tuple(SimpleNamespace(**s.model_dump()) for s in source.specifications)
    prepared = ww.Prepared(ALICE, lines, source.render()[0], RULE_SET, source)
    monkeypatch.setattr(ww, "prepare", AsyncMock(return_value=prepared))
    settled = AsyncMock(return_value=True)
    monkeypatch.setattr(ww, "settle", settled)
    draft = AsyncMock(side_effect=AssertionError("no quotation may be created"))
    monkeypatch.setattr(quote_drafts, "create_quote_draft", draft)
    database = SimpleNamespace(session=Mock(side_effect=AssertionError("no database")))
    router, captured = Router(), []
    real = sv.run_supervisor_workflow

    async def supervise(ctx):
        result = await real(ctx)
        captured.append(result)
        return result

    monkeypatch.setattr(sv, "run_supervisor_workflow", supervise)
    settings = ww.WorkerSettings()
    run = ww.Claim("new-run", "RFQ-new", ALICE.customer_id, 1, 1)
    deps = ww.Dependencies(context(source).intake_graph, lambda principal: router)
    assert await ww.process(database, settings, deps, run) == "blocked"
    (result,) = captured
    assert result.route.stopped_at == "manufacturing"
    assert result.disposition.category == "not_quotable"
    assert not result.draft_available and not result.approved and not result.released
    assert "check_inventory" not in router.calls and "estimate_costs" not in router.calls
    assert settled.await_args.args[4:7] == ("blocked", "not_quotable", None)
    draft.assert_not_awaited()
    database.session.assert_not_called()


async def test_source_gate_still_refuses_altered_values_and_invented_parts():
    source = structured()
    checked = await validate(source)
    spec = checked.parts[0].specification
    execute = AsyncMock(return_value="ok")
    line = SimpleNamespace(**source.specifications[0].model_dump())
    gate = ww.SourceGate(execute, (line,))
    for update in ({"width_mm": Decimal("65")}, {"part_id": "P-INVENTED"}):
        with pytest.raises(McpCallError):
            await gate(
                "check_manufacturing_feasibility",
                PartCheckInput(part=spec.model_copy(update=update)),
            )
    assert gate.mismatch
    execute.assert_not_awaited()


async def test_structured_provenance_does_not_accept_a_proposal_schema_override():
    with pytest.raises(ValidationError):
        intake.ExtractionProposal.model_validate({"parts": [], "structured_source": structured()})
    source = structured()
    ctx = context(source, source_text=source.render()[0] + "Notes: Width 65 mm.")
    result = await sv.run_supervisor_workflow(ctx)
    assert result.status == "error" and result.error == "invalid_source"
    assert not result.draft_available


async def test_structured_injection_detection_still_requires_review_before_a2():
    source = structured(specification(part_id="P-approve-002"))
    router = Router()
    result = await sv.run_supervisor_workflow(context(source, router))
    assert result.route.stopped_at == "intake_validation" and result.route.action == "review"
    assert "check_manufacturing_feasibility" not in router.calls
    assert not result.draft_available


async def test_structured_input_still_screens_credentials_before_the_graph():
    source = structured(specification(part_id="eyJhbGciOiJIUzI1NiJ9.SECRET-TOKEN.signature"))
    graph = SimpleNamespace(ainvoke=AsyncMock())
    result = await extract(source, graph)
    assert result.status == "error" and result.error == "unsafe_content"
    graph.ainvoke.assert_not_awaited()


async def test_genuinely_unstructured_input_still_calls_and_verifies_the_model(monkeypatch):
    invocation = AsyncMock(return_value=intake.ExtractionProposal.model_validate(complete_args()))
    monkeypatch.setattr(llm, "invoke_structured", invocation)
    graph = intake.build_intake_graph(model(reply({"parts": []})))
    extracted = await intake.run_intake(graph, COMPLETE, source_id="unstructured")
    checked = await a1v.validate_intake(extracted, COMPLETE, rules=RULES, catalog=Catalog())
    invocation.assert_awaited_once()
    assert checked.readiness == "ready_for_manufacturing_evaluation"


async def test_unstructured_duplicate_candidates_still_require_clarification(monkeypatch):
    proposal = complete_args()
    proposal["parts"] *= 2
    invocation = AsyncMock(return_value=intake.ExtractionProposal.model_validate(proposal))
    monkeypatch.setattr(llm, "invoke_structured", invocation)
    graph = intake.build_intake_graph(model(reply({"parts": []})))
    extracted = await intake.run_intake(graph, COMPLETE, source_id="unstructured")
    checked = await a1v.validate_intake(extracted, COMPLETE, rules=RULES, catalog=Catalog())
    invocation.assert_awaited_once()
    assert len(extracted.parts) == len(checked.parts) == 2
    assert checked.readiness == "needs_clarification"
    assert all(p.specification is None for p in checked.parts)


@asynccontextmanager
async def session_scope(value=None):
    yield value


def prepare_database(monkeypatch, notes=None, lines=None):
    details = SimpleNamespace(
        runtime_lines=lines or (SimpleNamespace(**specification().model_dump()),),
        rfq=SimpleNamespace(source_type="customer_portal", raw_request=notes),
    )
    monkeypatch.setattr(repositories, "get_rfq", AsyncMock(return_value=details))
    monkeypatch.setattr(ww, "service_principal", AsyncMock(return_value=ALICE))
    pinned = {"mfg-v1.0": RULES, "price-v1.0": TRUSTED, "approval-v1.2": APPROVAL_V12}
    monkeypatch.setattr(runtime, "pinned_rules", AsyncMock(side_effect=lambda s, v: pinned[v]))
    session = SimpleNamespace(
        begin=lambda: session_scope(),
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(all=lambda: [("MAT-001", "aluminum", "5052-H32")]),
                SimpleNamespace(all=lambda: [("none", "Unfinished")]),
            ]
        ),
    )
    return SimpleNamespace(session=lambda: session_scope(session)), session


@pytest.mark.parametrize(
    "notes",
    [None, "Width 65 mm.", "Part P-OTHER: width 65 mm.", "Part P-OTHER: width 60 mm."],
)
async def test_prepare_selects_structured_facts_only_when_no_unstructured_requirements_exist(
    monkeypatch, notes
):
    database, session = prepare_database(monkeypatch, notes)
    prepared = await ww.prepare(database, ww.Claim("run", "RFQ-new", ALICE.customer_id, 1, 1))
    repositories.get_rfq.assert_awaited_once_with(session, "RFQ-new", customer_id=ALICE.customer_id)
    if notes is None:
        assert prepared.structured_source.specifications == (specification(),)
        assert prepared.text == prepared.structured_source.render()[0]
    else:
        assert prepared.structured_source is None
        assert prepared.text.endswith(f"Notes: {notes}")
        # Existing contradiction and ownership processing remains active for all notes.
        args = {
            "parts": [
                {
                    "label": PART_ID,
                    "description": ev("l bracket"),
                    "quantity": ev(20, "quantity 20"),
                    "material": ev("aluminum 5052-H32"),
                }
            ]
        }
        graph = intake.build_intake_graph(model(reply(args)))
        extracted = await intake.run_intake(graph, prepared.text, source_id="notes")
        checked = await a1v.validate_intake(
            extracted, prepared.text, rules=RULES, catalog=Catalog(), trusted_part_ids=(PART_ID,)
        )
        assert checked.readiness == "needs_clarification"
        assert checked.parts[0].specification is None
        if "P-OTHER" not in notes:
            assert any(
                c.field == "width" and c.kind == "contradiction" for c in checked.clarifications
            )
        else:
            assert extracted.part_boundaries == "unverified"
            assert any(c.kind == "ownership_unverified" for c in checked.clarifications)


async def test_omitted_part_in_notes_blocks_manufacturing_even_when_all_values_agree():
    source = structured()
    line = SimpleNamespace(**source.specifications[0].model_dump())
    materials, finishes = dict(source.material_names), dict(source.finish_names)
    text = ww.source_text([line], "Part P-OTHER: width 60 mm.", materials, finishes)
    # Every scalar needed by A1 is supplied correctly for the persisted bracket. The
    # other part repeats its width, so only ownership can block this otherwise ready
    # input. No second part may be synthesized or silently absorbed into the bracket.
    candidate = {
        "label": PART_ID,
        "description": ev("l bracket", "part type l bracket"),
        "quantity": ev(20, "quantity 20"),
        "material": ev("aluminum 5052-H32", "material aluminum 5052-H32"),
        "finish": ev("unfinished", "finish unfinished"),
        "length": ev("100 mm", "length 100 mm"),
        "width": ev("60 mm", "width 60 mm"),
        "height": ev("30 mm", "height 30 mm"),
        "thickness": ev("2 mm", "thickness 2 mm"),
        "tolerance": ev("0.2 mm", "tolerance 0.2 mm"),
        "hole_count": ev(0, "hole count 0"),
    }
    proposal = {"parts": [candidate]}
    control_text = ww.source_text([line], "Width 60 mm.", materials, finishes)
    control = await intake.run_intake(
        intake.build_intake_graph(model(reply(proposal))),
        control_text,
        source_id="structured-source",
        rfq_id="RFQ-new",
    )
    control_checked = await a1v.validate_intake(
        control, control_text, rules=RULES, catalog=Catalog(), trusted_part_ids=(PART_ID,)
    )
    assert control_checked.readiness == "ready_for_manufacturing_evaluation"
    assert control_checked.clarifications == ()

    graph = intake.build_intake_graph(model(reply(proposal)))
    extraction = await intake.run_intake(
        graph, text, source_id="structured-source", rfq_id="RFQ-new"
    )
    catalog = Catalog()
    checked = await a1v.validate_intake(
        extraction, text, rules=RULES, catalog=catalog, trusted_part_ids=(PART_ID,)
    )
    assert len(extraction.parts) == len(checked.parts) == 1
    assert extraction.part_boundaries == "unverified" and extraction.parts[0].scope is None
    assert checked.readiness == "needs_clarification" and checked.parts[0].specification is None
    assert any(c.kind == "ownership_unverified" for c in checked.clarifications)
    assert not any(c.kind == "contradiction" for c in checked.clarifications)
    assert catalog.calls == []

    router = Router()
    ctx = context(
        source,
        router,
        source_text=text,
        structured_source=None,
        intake_graph=intake.build_intake_graph(model(reply(proposal))),
    )
    result = await sv.run_supervisor_workflow(ctx)
    assert result.status == "executed"
    assert result.route.stopped_at == "intake_validation" and result.route.action == "clarification"
    assert router.calls == []  # no catalog, manufacturing, supply, pricing or quotation
    assert not result.draft_available and not result.approved and not result.released


@pytest.mark.parametrize(
    "bad_lines",
    [
        (SimpleNamespace(**(specification().model_dump() | {"length_mm": "100 or 150"})),),
        (
            SimpleNamespace(**specification().model_dump()),
            SimpleNamespace(**specification(width_mm="65").model_dump()),
        ),
    ],
)
async def test_prepare_refuses_invalid_or_contradictory_persisted_facts(monkeypatch, bad_lines):
    database, _ = prepare_database(monkeypatch, lines=bad_lines)
    with pytest.raises(ww.Unavailable) as refused:
        await ww.prepare(database, ww.Claim("run", "RFQ-new", ALICE.customer_id, 1, 1))
    assert refused.value.code == "invalid_structured_source"
    assert refused.value.disposition == "not_evaluated" and not refused.value.retry
    ww.service_principal.assert_not_awaited()
