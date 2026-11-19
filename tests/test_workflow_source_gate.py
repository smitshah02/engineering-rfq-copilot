"""Offline source-gate characterization and manufacturing-policy regression.

The gate's existing exact ID comparison and null-skipping behavior are intentional test
expectations here. Worker identity resolution happens in Intake validation, before the
unchanged exact gate. No database, live MCP, Ollama or external model is used.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_hardening_services import APPROVAL_V12
from test_intake import ev, model, proposal, reply
from test_intake_validation import CODED, CODED_ARGS, COMPLETE, Catalog, complete_args
from test_manufacturing_agent import service_result
from test_pricing_agent import TRUSTED
from test_supervisor import MFG, PRINCIPAL

from rfq_copilot import intake, quote_drafts
from rfq_copilot import supervisor as sv
from rfq_copilot import workflow_drafter as wd
from rfq_copilot import workflow_worker as ww
from rfq_copilot.api.schemas.rfq import PartSpecification
from rfq_copilot.intake_validation import validate_intake
from rfq_copilot.mcp.client import McpCallError, McpToolClient
from rfq_copilot.mcp.contracts import PartCheckInput, PlanningInput

pytestmark = pytest.mark.asyncio
PART_ID = "P-TEST-LIVE-006"
SPEC_FIELDS = (
    "part_type", "material_id", "finish_id", "length_mm", "width_mm", "height_mm",
    "thickness_mm", "tolerance_mm", "bend_radius_mm", "weld_length_mm", "quantity",
    "hole_count",
)  # fmt: skip


@pytest.fixture(autouse=True)
def no_live_mcp(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("use the injected offline executor")

    monkeypatch.setattr(McpToolClient, "call", forbidden)


def stored_line(**over):
    return SimpleNamespace(
        **{
            "part_id": PART_ID,
            "part_type": "flat_panel",
            "material_id": "MAT-001",
            "finish_id": "powder_coat",
            "length_mm": Decimal("300.000"),
            "width_mm": Decimal("200.000"),
            "height_mm": None,
            "thickness_mm": Decimal("2.000"),
            "tolerance_mm": Decimal("0.200"),
            "bend_radius_mm": None,
            "weld_length_mm": None,
            "quantity": 25,
            "hole_count": None,
            **over,
        }
    )


def specification(line, **over):
    return PartSpecification(
        **({name: getattr(line, name) for name in PartSpecification.model_fields} | over)
    )


def diagnostics(caplog):
    return [
        r for r in caplog.records
        if r.name == ww.__name__ and r.getMessage().startswith("event=workflow_source_mismatch")
    ]  # fmt: skip


def assert_diagnostic(caplog, field, category):
    (record,) = diagnostics(caplog)
    assert (
        record.getMessage() == f"event=workflow_source_mismatch field={field} category={category}"
    )
    assert record.exc_info is None and record.stack_info is None


@pytest.mark.parametrize(
    "part_id,matches",
    [
        (PART_ID, True),
        ("Part P-TEST-LIVE-006", False),
        ("p-test-live-006", False),
        ("P-test-LIVE-006", False),
        (" P-TEST-LIVE-006 ", False),
        ("\tP-TEST-LIVE-006\n", False),
        ("P-TEST-LIVE-0 06", False),
        ("P-TEST-LIVE- 006", False),
        ("P-INVENTED-999", False),
    ],
)
async def test_source_gate_part_identifiers_are_compared_verbatim(caplog, part_id, matches):
    line = stored_line()
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(execute, [line])
    request = PartCheckInput(part=specification(line, part_id=part_id))
    assert request.part.part_id == part_id  # the structural schema also preserves spelling
    if matches:
        assert await gate("check_manufacturing_feasibility", request) == "ok"
        execute.assert_awaited_once_with("check_manufacturing_feasibility", request)
        assert not gate.mismatch and not diagnostics(caplog)
    else:
        with pytest.raises(McpCallError) as refused:
            await gate("check_manufacturing_feasibility", request)
        assert refused.value.code == "invalid_input" and gate.mismatch
        execute.assert_not_awaited()
        assert_diagnostic(caplog, "part_id", "unknown_part_id")
        assert ww.classify(None, gate.mismatch) == ("blocked", "source_mismatch", "source_mismatch")


async def test_source_gate_rejects_an_identifier_that_belongs_only_to_another_rfq(caplog):
    current = stored_line(rfq_id="RFQ-CURRENT-PRIVATE")
    foreign = stored_line(part_id="P-OTHER-RFQ-001", rfq_id="RFQ-OTHER-PRIVATE")
    execute = AsyncMock(return_value="ok")
    request = PartCheckInput(part=specification(foreign))
    # Valid in its own RFQ; never expand the current RFQ's trusted set with foreign parts.
    other_gate = ww.SourceGate(execute, [foreign])
    await other_gate("check_manufacturing_feasibility", request)
    execute.reset_mock()
    gate = ww.SourceGate(execute, [current])
    with pytest.raises(McpCallError) as refused:
        await gate("check_manufacturing_feasibility", request)
    assert refused.value.code == "invalid_input" and gate.mismatch
    execute.assert_not_awaited()
    assert_diagnostic(caplog, "part_id", "unknown_part_id")


@pytest.mark.parametrize(
    "field,persisted,extracted",
    [
        ("tolerance_mm", "0.100", "0.1"),
        ("tolerance_mm", "0.1", "0.100"),
        ("width_mm", "100.000", "100"),
        ("width_mm", "100", "100.000"),
    ],
)
async def test_source_gate_decimal_equivalence_passes_without_diagnostics(
    caplog, field, persisted, extracted
):
    line = stored_line(**{field: Decimal(persisted)})
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(execute, [line])
    request = PartCheckInput(part=specification(line, **{field: Decimal(extracted)}))
    assert await gate("check_manufacturing_feasibility", request) == "ok"
    execute.assert_awaited_once_with("check_manufacturing_feasibility", request)
    assert not gate.mismatch and not diagnostics(caplog)


@pytest.mark.parametrize(
    "field,value",
    [
        ("width_mm", Decimal("201.125")),
        ("quantity", 26),
        ("material_id", "MAT-002"),
        ("finish_id", "brushed"),
        ("tolerance_mm", Decimal("0.125")),
        ("length_mm", Decimal("301")),
        ("thickness_mm", Decimal("3")),
        ("part_type", "mounting_plate"),
        ("height_mm", Decimal("51")),
        ("bend_radius_mm", Decimal("5")),
        ("weld_length_mm", Decimal("101")),
        ("hole_count", 4),
    ],
)
async def test_source_gate_value_conflicts_report_only_the_static_field(caplog, field, value):
    line = stored_line(
        height_mm=Decimal("50"), bend_radius_mm=Decimal("4"),
        weld_length_mm=Decimal("100"), hole_count=3,
    )  # fmt: skip
    execute = AsyncMock()
    gate = ww.SourceGate(execute, [line])
    with pytest.raises(McpCallError) as refused:
        await gate(
            "check_manufacturing_feasibility",
            PartCheckInput(part=specification(line, **{field: value})),
        )
    assert refused.value.code == "invalid_input" and gate.mismatch
    execute.assert_not_awaited()
    assert_diagnostic(caplog, field, "value_conflict")


@pytest.mark.parametrize("field", SPEC_FIELDS)
@pytest.mark.parametrize("omitted", [True, False], ids=["omitted", "explicit_null"])
async def test_source_gate_skips_missing_fields_even_when_persisted_non_null(
    caplog, field, omitted
):
    line = stored_line(
        height_mm=Decimal("50"), bend_radius_mm=Decimal("4"),
        weld_length_mm=Decimal("100"), hole_count=3,
    )  # fmt: skip
    data = specification(line).model_dump()
    assert data[field] is not None
    if omitted:
        del data[field]
    else:
        data[field] = None
    request = PartCheckInput(part=PartSpecification(**data))
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(execute, [line])
    # Existing gate behavior: completeness is checked elsewhere, not by SourceGate.
    assert await gate("check_manufacturing_feasibility", request) == "ok"
    execute.assert_awaited_once_with("check_manufacturing_feasibility", request)
    assert not gate.mismatch and not diagnostics(caplog)


async def test_source_gate_skips_values_not_stated_in_the_persisted_line(caplog):
    line = stored_line()
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(execute, [line])
    request = PartCheckInput(part=specification(line, hole_count=4))
    assert await gate("check_manufacturing_feasibility", request) == "ok"
    assert not gate.mismatch and not diagnostics(caplog)


@pytest.mark.parametrize("bad_index", [0, 1])
async def test_source_gate_one_conflicting_part_refuses_the_entire_batch(caplog, bad_index):
    lines = [stored_line(), stored_line(part_id="P-SECOND-002")]
    parts = [specification(line) for line in lines]
    parts[bad_index] = specification(lines[bad_index], width_mm=Decimal("201"))
    execute = AsyncMock()
    gate = ww.SourceGate(execute, lines)
    with pytest.raises(McpCallError) as refused:
        await gate("check_inventory", PlanningInput(parts=tuple(parts), horizon_start="2026-11-16"))
    assert refused.value.code == "invalid_input" and gate.mismatch
    execute.assert_not_awaited()
    assert_diagnostic(caplog, "width_mm", "value_conflict")


async def test_source_gate_logs_only_the_first_conflict_per_attempt(caplog):
    line = stored_line()
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(execute, [line])
    for over in (
        {"width_mm": Decimal("201"), "quantity": 26},
        {"width_mm": Decimal("201")},
        {"part_id": "P-INVENTED-999"},
        {"finish_id": "brushed"},
    ):
        with pytest.raises(McpCallError):
            await gate("check_manufacturing_feasibility", PartCheckInput(part=specification(line, **over)))  # fmt: skip
    execute.assert_not_awaited()
    assert_diagnostic(caplog, "width_mm", "value_conflict")
    # An agreeing call still executes, but cannot clear the attempt's historical mismatch.
    await gate("check_manufacturing_feasibility", PartCheckInput(part=specification(line)))
    assert gate.mismatch
    assert_diagnostic(caplog, "width_mm", "value_conflict")
    # A new attempt gets its own diagnostic allowance.
    next_gate = ww.SourceGate(execute, [line])
    with pytest.raises(McpCallError):
        await next_gate("check_manufacturing_feasibility", PartCheckInput(part=specification(line, quantity=26)))  # fmt: skip
    assert len(diagnostics(caplog)) == 2


async def test_source_gate_diagnostics_contain_no_customer_data_or_specification_values(caplog):
    line = stored_line(
        part_id="P-PRIVATE-PERSISTED", rfq_id="RFQ-PRIVATE", customer_id="CUST-PRIVATE",
        raw_request="Customer Private Name requests a confidential project",
        width_mm=Decimal("237.125"), tolerance_mm=Decimal("0.175"), quantity=73,
        material_id="MAT-PRIVATE", finish_id="FINISH-PRIVATE",
    )  # fmt: skip
    execute = AsyncMock()
    for over, field, category in (
        ({"width_mm": Decimal("238.875")}, "width_mm", "value_conflict"),
        (
            {"part_id": "P-PRIVATE-MODEL\nevent=injected token=private"},
            "part_id",
            "unknown_part_id",
        ),  # fmt: skip
    ):
        caplog.clear()
        gate = ww.SourceGate(execute, [line])
        with pytest.raises(McpCallError):
            await gate("check_manufacturing_feasibility", PartCheckInput(part=specification(line, **over)))  # fmt: skip
        assert_diagnostic(caplog, field, category)
        (record,) = diagnostics(caplog)
        assert record.args == (field, category)  # no hidden request, value or exception args
        blob = repr((record.msg, record.args, record.exc_info, record.stack_info))
        private = [str(v) for v in vars(line).values() if v is not None]
        private += [str(v) for v in over.values()]
        assert not any(value in blob for value in private)
    execute.assert_not_awaited()


async def test_source_gate_unknown_schema_field_uses_static_fallback(caplog):
    class ExtendedSpecification(PartSpecification):
        private_customer_field: str

    line = stored_line(private_customer_field="private persisted value")
    spec = ExtendedSpecification(
        **specification(line).model_dump(), private_customer_field="private model value"
    )
    execute = AsyncMock()
    gate = ww.SourceGate(execute, [line])
    with pytest.raises(McpCallError):
        await gate("check_manufacturing_feasibility", PartCheckInput(part=spec))
    execute.assert_not_awaited()
    assert_diagnostic(caplog, "specification", "value_conflict")


@pytest.mark.parametrize(
    "label,validated_id,matches",
    [
        (PART_ID, PART_ID, True),
        ("Part P-TEST-LIVE-006", "Part P-TEST-LIVE-006", False),
        ("p-test-live-006", "p-test-live-006", False),
        (" P-TEST-LIVE-006 ", " P-TEST-LIVE-006 ", False),
        ("  Part   p-test-live-006  ", "  Part   p-test-live-006  ", False),
        ("P-TEST-LIVE-0 06", PART_ID, True),
        ("P-TEST-LIVE- 006", PART_ID, True),
    ],
)
@pytest.mark.parametrize("resolve", [False, True], ids=["generic_intake", "trusted_rfq_ids"])
async def test_source_gate_after_a1_label_verification(
    caplog, label, validated_id, matches, resolve
):
    # Same prefix as the worker's deterministic rendering; only the extraction is mocked.
    text = COMPLETE.replace("P-100:", f"Part {PART_ID}:")
    extraction = intake.verify_extraction(
        proposal(complete_args(label=label)), text, "src-1", "RFQ-1"
    )
    assert extraction.parts[0].label_verified
    original = extraction.model_dump()
    kwargs = {"trusted_part_ids": (PART_ID,)} if resolve else {}
    validation = await validate_intake(extraction, text, rules=MFG, catalog=Catalog(), **kwargs)
    assert validation.readiness == "ready_for_manufacturing_evaluation"
    part = validation.parts[0].specification
    assert part is not None and part.part_id == (PART_ID if resolve else validated_id)
    assert validation.parts[0].label == validated_id
    assert extraction.model_dump() == original  # original labels, evidence and scopes survive
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(execute, [stored_line()])
    request = PartCheckInput(part=part)
    if matches or resolve:
        assert await gate("check_manufacturing_feasibility", request) == "ok"
        assert not gate.mismatch and not diagnostics(caplog)
    else:
        with pytest.raises(McpCallError) as refused:
            await gate("check_manufacturing_feasibility", request)
        assert refused.value.code == "invalid_input" and gate.mismatch
        execute.assert_not_awaited()
        assert_diagnostic(caplog, "part_id", "unknown_part_id")


@pytest.mark.parametrize("label", [PART_ID, f"Part {PART_ID}", PART_ID.lower(), f" {PART_ID} "])
async def test_source_gate_consistent_tight_tolerance_is_not_quotable_not_source_mismatch(
    monkeypatch, caplog, label
):
    """Real A1 validation, A2 services, pinned rules, Supervisor and worker settlement map."""
    line = stored_line(tolerance_mm=Decimal("0.100"))
    trusted_spec = specification(line)
    assert MFG.document["limits"]["min_tolerance_mm"] == Decimal("0.2")
    assert sv._manufacturing_trusted(MFG)  # no bypass of the manifest/fingerprint checks
    text = COMPLETE.replace("P-100:", f"Part {PART_ID}:").replace("±0.2 mm", "±0.100 mm")
    args = complete_args(label=label, tolerance=ev("±0.100 mm", "tolerance ±0.100 mm"))
    catalog = Catalog()
    manufacturing_outputs = []
    calls = []

    async def execute(tool, request):
        calls.append(tool)
        if tool == "search_materials":
            return await catalog(tool, request)
        assert tool == "check_manufacturing_feasibility"  # no supply, pricing or quotation
        assert request.part == trusted_spec  # every extracted value agrees with persistence
        # The offline tool maps the real pure material and manufacturing services exactly
        # as MCP does, with the real pinned rule rather than the harness's dummy fingerprint.
        out = service_result(request.part, rules=MFG)
        manufacturing_outputs.append(out)
        return out

    prepared = ww.Prepared(
        PRINCIPAL, (line,), text,
        {"manufacturing": MFG, "pricing": TRUSTED, "approval": APPROVAL_V12},
    )  # fmt: skip
    monkeypatch.setattr(ww, "prepare", AsyncMock(return_value=prepared))
    settled = AsyncMock(return_value=True)
    monkeypatch.setattr(ww, "settle", settled)
    create_draft = AsyncMock(side_effect=AssertionError("no internal quotation may be created"))
    monkeypatch.setattr(quote_drafts, "create_quote_draft", create_draft)
    monkeypatch.setattr(wd, "create_quote_draft", create_draft)
    real_supervisor = sv.run_supervisor_workflow
    results, gates = [], []

    async def supervise(context):
        assert context.trusted_part_ids == (PART_ID,)
        gates.append(context.execute)
        result = await real_supervisor(context)
        results.append(result)
        return result

    monkeypatch.setattr(sv, "run_supervisor_workflow", supervise)
    database = SimpleNamespace(session=Mock(side_effect=AssertionError("no database access")))
    settings = ww.WorkerSettings()
    run = ww.Claim("run-offline", "RFQ-1", PRINCIPAL.customer_id, 1, 1)
    graph = intake.build_intake_graph(model(reply(args)))
    deps = ww.Dependencies(graph, lambda principal: execute)
    assert await ww.process(database, settings, deps, run) == "blocked"
    (gate,) = gates
    (result,) = results
    (out,) = manufacturing_outputs
    assert not gate.mismatch and not diagnostics(caplog)
    assert out.result == "unsupported" and out.findings == ("TOLERANCE_UNSUPPORTED",)
    assert (out.rule_version, out.rule_fingerprint) == ("mfg-v1.0", MFG.fingerprint)
    assert result.status == "executed"
    assert result.disposition.category == "not_quotable"
    assert result.disposition.blocking_findings == ("TOLERANCE_UNSUPPORTED",)
    assert ww.classify(result, gate.mismatch) == ("blocked", "not_quotable", None)
    assert not result.draft_available and not result.approved and not result.released
    assert calls.count("check_manufacturing_feasibility") == 1
    settled.assert_awaited_once_with(
        database, settings, run, PRINCIPAL.user_id, "blocked", "not_quotable", None,
        result.attempt_id, clarification_codes=(),
    )  # fmt: skip
    create_draft.assert_not_awaited()
    database.session.assert_not_called()


async def resolve_source(text, args, trusted_part_ids):
    extraction = intake.verify_extraction(proposal(args), text, "src-1", "RFQ-1")
    validation = await validate_intake(
        extraction, text, rules=MFG, catalog=Catalog(), trusted_part_ids=trusted_part_ids
    )
    return extraction, validation


@pytest.mark.parametrize("stored_id", [PART_ID, PART_ID.lower(), f" {PART_ID} "])
async def test_trusted_id_resolution_preserves_persisted_spelling(caplog, stored_id):
    text = COMPLETE.replace("P-100:", f"Part {stored_id}:")
    label = f"part {PART_ID.lower()}"  # one explicit worker wrapper, matched against trusted IDs
    extraction, validation = await resolve_source(text, complete_args(label=label), (stored_id,))
    assert extraction.parts[0].label_verified
    assert validation.readiness == "ready_for_manufacturing_evaluation"
    part = validation.parts[0]
    assert part.label == label and part.specification.part_id == stored_id
    line = stored_line(part_id=stored_id)
    before = vars(line).copy()
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(execute, [line])
    assert await gate("check_manufacturing_feasibility", PartCheckInput(part=part.specification)) == "ok"  # fmt: skip
    assert vars(line) == before and not gate.mismatch and not diagnostics(caplog)


@pytest.mark.parametrize(
    "trusted_ids,label",
    [
        ((PART_ID, PART_ID.lower()), PART_ID),
        ((PART_ID, PART_ID.lower()), f"Part {PART_ID}"),
        ((PART_ID, f" {PART_ID} "), PART_ID),
        ((PART_ID, f"Part {PART_ID}"), f"Part {PART_ID}"),
    ],
)
async def test_trusted_id_resolution_refuses_colliding_aliases_even_for_exact_ids(
    trusted_ids, label
):
    text = COMPLETE.replace("P-100:", f"Part {PART_ID}:")
    extraction, validation = await resolve_source(text, complete_args(label=label), trusted_ids)
    assert extraction.parts[0].label_verified
    assert validation.readiness == "needs_clarification"
    assert validation.parts[0].specification is None
    assert any(
        f.field == "part_id" and f.kind == "ownership_unverified"
        for f in validation.parts[0].findings
    )


@pytest.mark.parametrize("label", ["P-INVENTED-999", "P-FOREIGN-RFQ-001", "Item P-100"])
async def test_trusted_id_resolution_preserves_source_gate_refusal_for_untrusted_ids(caplog, label):
    # Being present in customer text is insufficient: membership comes only from persistence.
    text = COMPLETE.replace("P-100:", f"{label}:")
    extraction, validation = await resolve_source(text, complete_args(label=label), (PART_ID,))
    assert extraction.parts[0].label_verified
    assert validation.readiness == "ready_for_manufacturing_evaluation"
    part = validation.parts[0].specification
    assert part.part_id == label  # no guessed mapping to a known part
    execute = AsyncMock()
    gate = ww.SourceGate(execute, [stored_line()])
    with pytest.raises(McpCallError) as refused:
        await gate("check_manufacturing_feasibility", PartCheckInput(part=part))
    assert refused.value.code == "invalid_input" and gate.mismatch
    execute.assert_not_awaited()
    assert_diagnostic(caplog, "part_id", "unknown_part_id")
    assert ww.classify(None, gate.mismatch) == ("blocked", "source_mismatch", "source_mismatch")


async def test_trusted_id_resolution_cannot_make_an_absent_label_source_verified():
    text = COMPLETE.replace("P-100:", f"Part {PART_ID}:")
    extraction, validation = await resolve_source(
        text, complete_args(label="Part P-INVENTED-999"), (PART_ID,)
    )
    assert not extraction.parts[0].label_verified
    assert validation.readiness != "ready_for_manufacturing_evaluation"
    assert validation.parts[0].specification is None


async def test_trusted_id_resolution_does_not_strip_arbitrary_or_repeated_wrappers():
    text = COMPLETE.replace("P-100:", f"Part Part {PART_ID}:")
    _, validation = await resolve_source(text, complete_args(label=f"Part Part {PART_ID}"), (PART_ID,))  # fmt: skip
    assert validation.readiness != "ready_for_manufacturing_evaluation"
    assert validation.parts[0].specification is None


async def test_trusted_id_resolution_empty_authoritative_set_fails_closed(caplog):
    text = COMPLETE.replace("P-100", PART_ID)
    _, validation = await resolve_source(text, complete_args(label=PART_ID), ())
    assert validation.readiness == "ready_for_manufacturing_evaluation"
    execute = AsyncMock()
    gate = ww.SourceGate(execute, [])
    with pytest.raises(McpCallError):
        await gate(
            "check_manufacturing_feasibility",
            PartCheckInput(part=validation.parts[0].specification),
        )
    assert gate.mismatch
    execute.assert_not_awaited()
    assert_diagnostic(caplog, "part_id", "unknown_part_id")


async def test_trusted_id_resolution_keeps_original_source_hash_check():
    extraction = intake.verify_extraction(proposal(complete_args()), COMPLETE, "src-1", "RFQ-1")
    with pytest.raises(ValueError, match="text is not the source"):
        await validate_intake(
            extraction, COMPLETE + " changed", rules=MFG, catalog=Catalog(),
            trusted_part_ids=("P-100",),
        )  # fmt: skip


def two_part_representations():
    text = CODED.replace("BRK-200:", "Part BRK-200:").replace("BRK-201:", "Part BRK-201:")
    args = {"parts": [
        CODED_ARGS["parts"][0] | {"label": "part brk-200"},
        CODED_ARGS["parts"][1] | {"label": " BRK-201 "},
    ]}  # fmt: skip
    return text, args


async def test_trusted_id_resolution_multiple_parts_keep_evidence_and_own_specifications(caplog):
    text, args = two_part_representations()
    extraction, validation = await resolve_source(text, args, ("BRK-200", "BRK-201"))
    original = await validate_intake(extraction, text, rules=MFG, catalog=Catalog())
    assert extraction.part_boundaries == "labelled"
    assert validation.readiness == "ready_for_manufacturing_evaluation"
    assert [p.specification.part_id for p in validation.parts] == ["BRK-200", "BRK-201"]
    for old, new in zip(original.parts, validation.parts, strict=True):
        assert new.label == old.label and new.derived == old.derived
        assert new.verified == old.verified
        assert new.specification.model_dump(exclude={"part_id"}) == old.specification.model_dump(exclude={"part_id"})  # fmt: skip
    lines = [
        stored_line(part_id="BRK-200", quantity=100, finish_id="none"),
        stored_line(
            part_id="BRK-201",
            quantity=50,
            material_id="MAT-002",
            finish_id="none",
            length_mm=Decimal("400"),
            width_mm=Decimal("100"),
            thickness_mm=Decimal("3"),
            tolerance_mm=Decimal("0.100"),
        ),  # fmt: skip
    ]
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(execute, lines)
    request = PlanningInput(
        parts=tuple(p.specification for p in validation.parts), horizon_start="2026-11-16"
    )
    assert await gate("check_inventory", request) == "ok"
    execute.assert_awaited_once_with("check_inventory", request)
    assert not gate.mismatch and not diagnostics(caplog)


async def test_trusted_id_resolution_cannot_borrow_a_missing_width_from_another_part():
    text, args = two_part_representations()
    text = text.replace("200 mm wide, ", "", 1)
    args["parts"][0] = args["parts"][0] | {"width": ev("100 mm", "100 mm wide")}
    extraction, validation = await resolve_source(text, args, ("BRK-200", "BRK-201"))
    width = extraction.parts[0].get("width")
    assert width.status == "unverified" and width.reason == "evidence_outside_part"
    assert validation.readiness == "needs_clarification"
    assert validation.parts[0].specification is None
    assert validation.parts[1].specification.part_id == "BRK-201"
    assert validation.parts[1].specification.width_mm == Decimal("100")


async def test_trusted_id_resolution_rejects_a_scope_that_spans_an_omitted_trusted_part():
    # Intake's whole-word pattern can find P-200 inside A-P-200. Omitting the first label
    # groups both source sections under P-200; equal values alone cannot prove ownership.
    text = " ".join(
        COMPLETE.replace("P-100:", f"Part {part_id}:") for part_id in ("A-P-200", "P-200", "P-300")
    )
    args = {"parts": [
        complete_args(label="P-200")["parts"][0],
        complete_args(label="Part P-300")["parts"][0],
    ]}  # fmt: skip
    extraction, validation = await resolve_source(text, args, ("A-P-200", "P-200", "P-300"))
    assert extraction.part_boundaries == "labelled"
    original = await validate_intake(extraction, text, rules=MFG, catalog=Catalog())
    assert original.readiness == "ready_for_manufacturing_evaluation"  # equal values hide it
    assert validation.readiness == "needs_clarification"
    assert validation.parts[0].specification is None


async def test_trusted_id_resolution_never_merges_two_extracted_parts_into_one():
    # Defence in depth at the trusted-result boundary: even if a future extractor verified
    # two distinct label representations, they must not resolve to the same stored part.
    text = COMPLETE.replace("P-100:", f"Part {PART_ID}:")
    extraction = intake.verify_extraction(
        proposal(complete_args(label=PART_ID)), text, "src-1", "RFQ-1"
    )
    duplicate = extraction.parts[0].model_copy(update={"index": 2, "label": f"Part {PART_ID}"})
    extraction = extraction.model_copy(update={
        "parts": (extraction.parts[0], duplicate), "part_boundaries": "labelled",
    })  # fmt: skip
    validation = await validate_intake(
        extraction, text, rules=MFG, catalog=Catalog(), trusted_part_ids=(PART_ID,)
    )
    assert validation.readiness == "needs_clarification"
    assert all(p.specification is None for p in validation.parts)


@pytest.mark.parametrize(
    "field,persisted",
    [
        ("width_mm", Decimal("201")),
        ("quantity", 26),
        ("material_id", "MAT-002"),
        ("finish_id", "none"),
        ("tolerance_mm", Decimal("0.100")),
    ],
)
async def test_trusted_id_resolution_does_not_hide_true_field_conflicts(caplog, field, persisted):
    text = COMPLETE.replace("P-100:", f"Part {PART_ID}:")
    _, validation = await resolve_source(text, complete_args(label=f"part {PART_ID.lower()}"), (PART_ID,))  # fmt: skip
    assert validation.readiness == "ready_for_manufacturing_evaluation"
    part = validation.parts[0].specification
    assert part.part_id == PART_ID
    execute = AsyncMock()
    gate = ww.SourceGate(execute, [stored_line(**{field: persisted})])
    with pytest.raises(McpCallError) as refused:
        await gate("check_manufacturing_feasibility", PartCheckInput(part=part))
    assert refused.value.code == "invalid_input" and gate.mismatch
    execute.assert_not_awaited()
    assert_diagnostic(caplog, field, "value_conflict")
    assert ww.classify(None, gate.mismatch) == ("blocked", "source_mismatch", "source_mismatch")


async def test_trusted_id_resolution_preserves_decimal_equivalence(caplog):
    text = COMPLETE.replace("P-100:", f"Part {PART_ID}:")
    text = text.replace("200 mm wide", "100.000 mm wide").replace("±0.2 mm", "±0.100 mm")
    args = complete_args(
        label=f"Part {PART_ID}", width=ev("100.000 mm", "100.000 mm wide"),
        tolerance=ev("±0.100 mm", "tolerance ±0.100 mm"),
    )  # fmt: skip
    _, validation = await resolve_source(text, args, (PART_ID,))
    assert validation.readiness == "ready_for_manufacturing_evaluation"
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(
        execute, [stored_line(width_mm=Decimal("100"), tolerance_mm=Decimal("0.1"))]
    )
    request = PartCheckInput(part=validation.parts[0].specification)
    assert await gate("check_manufacturing_feasibility", request) == "ok"
    execute.assert_awaited_once_with("check_manufacturing_feasibility", request)
    assert not gate.mismatch and not diagnostics(caplog)
