"""Day 36/37 optional real-LLM check of the Intake Agent: local Ollama only.

Opt-in (``RFQ_OLLAMA_SMOKE=1``, marked ``integration``, like ``test_ollama_smoke``). Requires
``ollama serve`` with ``qwen3:4b-instruct-2507-q4_K_M``. No database, no MCP host, no hosted
provider, no tracing, no retries.

Runs the fixed evaluation set (``intake_eval_set``) and Day 37 validation (pinned rule
document and catalog from the frozen dataset, catalog served at the tool boundary). Safety is
asserted strictly (no incorrect trusted value). Usefulness is asserted with a floor: failing
safely with nothing verified is not a functional pass. Metrics are printed.
"""

import os

import pytest
from intake_eval_set import CASES, score
from test_intake_validation import RULES, Catalog

from rfq_copilot import intake
from rfq_copilot.intake_validation import validate_intake

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("RFQ_OLLAMA_SMOKE") != "1", reason="opt-in: RFQ_OLLAMA_SMOKE=1"
    ),  # fmt: skip
]
MODEL = "qwen3:4b-instruct-2507-q4_K_M"
MIN_RECALL = 0.6  # measured 0.88 on Day 37; a floor, not a target


def ollama():
    from langchain_ollama import ChatOllama

    os.environ["LANGSMITH_TRACING"] = "false"
    return ChatOllama(model=MODEL, temperature=0, seed=7, reasoning=False, num_ctx=8192)


def assert_safe(text: str, result: intake.IntakeResult) -> None:
    assert result.readiness == "pending_validation"
    for f in [*result.rfq_fields, *(f for p in result.parts for f in p.fields)]:
        if f.status == "verified":
            start, end = f.span
            assert text[start:end] == f.evidence
            assert f.normalized is not None


async def test_real_model_intake_evaluation_set():
    totals = {"correct": 0, "wrong": 0, "gold": 0, "part_count_ok": 0, "extra_parts": 0}
    readiness = {}
    for name, (text, gold) in CASES.items():
        result = await intake.run_intake(intake.build_intake_graph(ollama()), text, source_id=f"eval-{name}")  # fmt: skip
        assert result.status != "error", (name, result.error)
        assert_safe(text, result)
        s = score(result, gold)
        for key in ("correct", "wrong", "gold"):
            totals[key] += s[key]
        totals["part_count_ok"] += s["parts"] == s["gold_parts"]
        totals["extra_parts"] += max(0, s["parts"] - s["gold_parts"])
        v = await validate_intake(result, text, rules=RULES, catalog=Catalog())
        readiness[name] = v.readiness
        print(f"\n[{name}] {s} readiness={v.readiness}")
        for c in v.clarifications:
            print(f"    ? {c.question}")
    precision = totals["correct"] / max(1, totals["correct"] + totals["wrong"])
    recall = totals["correct"] / max(1, totals["gold"])
    print(f"\nTOTAL {totals} precision={precision:.2f} recall={recall:.2f}")
    assert totals["wrong"] == 0  # never a trusted wrong value
    assert recall >= MIN_RECALL
    assert readiness["complete"] == "ready_for_manufacturing_evaluation"
    for name in ("incomplete", "ambiguous", "correction", "malicious"):
        assert readiness[name] != "ready_for_manufacturing_evaluation", name
