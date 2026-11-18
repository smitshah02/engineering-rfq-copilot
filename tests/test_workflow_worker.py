"""Checkpoint A3, offline: outcome classification, the source gate, the source text and the
worker's configuration. No database, network or model (the database paths are in
test_workflow_worker_database)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_supervisor_delegation import ALICE

from rfq_copilot import escalation as esc
from rfq_copilot import supervisor as sv
from rfq_copilot import workflow_worker as ww
from rfq_copilot.api.schemas.rfq import PartSpecification
from rfq_copilot.mcp.client import McpCallError
from rfq_copilot.mcp.contracts import PartCheckInput, PlanningInput

LINE = SimpleNamespace(
    part_id="P-100", part_type="flat_panel", material_id="MAT-001", finish_id="powder_coat",
    length_mm=Decimal("300.000"), width_mm=Decimal("200"), height_mm=None,
    thickness_mm=Decimal("2.0"), tolerance_mm=Decimal("0.2"), bend_radius_mm=None,
    weld_length_mm=None, quantity=25, hole_count=None,
)  # fmt: skip


def failed(code):
    return sv.SupervisorExecutionResult(status="error", error=code, disposition=esc.for_result(None, code, False))  # fmt: skip


@pytest.mark.parametrize(
    "code,expected",
    [
        ("internal_error", ("retry", None, "internal_error")),
        ("execution_limit", ("retry", None, "execution_limit")),
        (
            "approval_rules_untrusted",
            ("blocked", "configuration_error", "approval_rules_untrusted"),
        ),  # fmt: skip
        ("unauthenticated", ("blocked", "configuration_error", "unauthenticated")),
        ("invalid_source", ("blocked", "not_evaluated", "invalid_source")),
    ],
)
def test_supervisor_errors_split_into_retryable_and_final(code, expected):
    assert ww.classify(failed(code), mismatch=False) == expected


def test_a_source_mismatch_overrides_every_other_outcome():
    assert ww.classify(failed("internal_error"), mismatch=True) == ("blocked", "source_mismatch", "source_mismatch")  # fmt: skip


def test_source_text_is_deterministic_and_keeps_the_notes_verbatim():
    text = ww.source_text([LINE], "Rush, please.", {"MAT-001": "aluminum 5052-H32"}, {"powder_coat": "powder coat"})  # fmt: skip
    assert text == (
        "Part P-100: quantity 25, part type flat panel, material aluminum 5052-H32, finish powder coat, "
        "length 300 mm, width 200 mm, thickness 2 mm, tolerance 0.2 mm.\nNotes: Rush, please."
    )
    assert ww.source_text([LINE], None) == ww.source_text([LINE], None)  # no notes: lines only


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, tool, request):
        self.calls.append(tool)
        return "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec",
    [
        {"part_id": "P-100", "quantity": 30},  # contradicts a stored value
        {"part_id": "P-100", "material_id": "MAT-002"},
        {"part_id": "P-999", "quantity": 25},  # a part the RFQ never named
    ],
)
async def test_gate_refuses_contradictions_and_invented_parts_before_the_tool(spec):
    inner = Recorder()
    gate = ww.SourceGate(inner, [LINE])
    with pytest.raises(McpCallError) as refused:
        await gate(
            "check_manufacturing_feasibility", PartCheckInput(part=PartSpecification(**spec))
        )
    assert refused.value.code == "invalid_input" and gate.mismatch and inner.calls == []


@pytest.mark.asyncio
async def test_gate_passes_agreeing_specs_and_requests_without_parts():
    inner = Recorder()
    gate = ww.SourceGate(inner, [LINE])
    agreeing = PartSpecification(part_id="P-100", quantity=25, thickness_mm=Decimal("2"), hole_count=4)  # fmt: skip
    await gate("check_manufacturing_feasibility", PartCheckInput(part=agreeing))  # 2 == 2.0
    await gate("check_inventory", PlanningInput(parts=(agreeing,), horizon_start="2026-11-16"))
    await gate("search_materials", SimpleNamespace())
    assert inner.calls == ["check_manufacturing_feasibility", "check_inventory", "search_materials"]
    assert not gate.mismatch  # a value only the notes state (hole_count) is A1-verified text


@pytest.mark.parametrize(
    "over",
    [
        {"concurrency": 0},
        {"concurrency": 5},
        {"lease_seconds": 50, "heartbeat_seconds": 30},
        {"lease_seconds": 1000},
        {"worker_id": ""},
    ],  # fmt: skip
)
def test_worker_settings_refuse_unsafe_bounds(over):
    with pytest.raises(ValueError):
        ww.WorkerSettings(**over)


def test_only_a_local_ollama_model_can_be_configured(monkeypatch):
    with pytest.raises(SystemExit, match="ollama"):
        ww._intake_graph_from_env({"RFQ_WORKFLOW_LLM": "openai"})
    graph = ww._intake_graph_from_env({"RFQ_WORKFLOW_LLM": "ollama", "RFQ_WORKFLOW_OLLAMA_MODEL": "qwen3:8b"})  # fmt: skip
    assert graph is not None  # built only: no model call happens until a run


def test_the_worker_executor_never_forwards_a_rest_credential():
    sent = {}

    class Integration:
        async def call(self, principal, tool, request, *, rest_credential):
            sent.update(principal=principal, tool=tool, rest=rest_credential)
            return "ok"

    import asyncio

    asyncio.run(ww.mcp_executor(Integration(), ALICE)("search_materials", {}))
    assert sent == {"principal": ALICE, "tool": "search_materials", "rest": ""}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    __import__("os").environ.get("RFQ_OLLAMA_SMOKE") != "1", reason="opt-in: RFQ_OLLAMA_SMOKE=1"
)
async def test_ollama_smoke_worker_intake_through_the_supervisor(monkeypatch):
    """Opt-in, local only: the worker's configured Ollama intake graph (no paid model), the
    real Supervisor A1 -> A5 and the SourceGate; MCP tools are the offline fake router."""
    from test_intake_ollama import MODEL
    from test_intake_validation import COMPLETE, RULES
    from test_pricing_agent import TRUSTED
    from test_quotation_agent import APPROVAL_V12
    from test_specialized_agent_integration import Router
    from test_supply_agent import MONDAY

    original = sv._manufacturing_trusted
    monkeypatch.setattr(sv, "_manufacturing_trusted", lambda r: r is RULES or original(r))
    graph = ww._intake_graph_from_env({"RFQ_WORKFLOW_LLM": "ollama", "RFQ_WORKFLOW_OLLAMA_MODEL": MODEL})  # fmt: skip
    router = Router()
    gate = ww.SourceGate(router, [LINE])
    text = ww.source_text([LINE], COMPLETE, {"MAT-001": "aluminum 5052-H32"}, {"powder_coat": "powder coat"})  # fmt: skip
    context = sv.SupervisorContext(
        principal=ALICE, execute=gate, rfq_id="RFQ-1", source_id="RFQ-1", source_text=text,
        manufacturing_rules=RULES, pricing_rules=TRUSTED, approval_rules=APPROVAL_V12,
        intake_graph=graph, horizon_start=MONDAY,
    )  # fmt: skip
    result = await sv.run_supervisor_workflow(context)
    status, disposition, error = ww.classify(result, gate.mismatch)
    print({"status": status, "disposition": disposition, "error": error, "tools": router.calls})
    assert result.status == "executed" and not result.approved and not result.released
    assert status != "retry" and disposition != "source_mismatch", (status, disposition, error)
