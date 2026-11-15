"""Day 50 live Langfuse smoke test: opt-in only (``RFQ_LANGFUSE_SMOKE=1`` AND
``RFQ_LANGFUSE_ENABLED=true`` with the Langfuse variables exported). Sends two synthetic
traces (one success, one controlled tool failure) from the offline Phase 6 harness: no
database, no MCP server, no model, no customer data. Prints each run id and the exact metadata
sent so the payload can be compared with what Langfuse shows. Never part of the default run.

    RFQ_LANGFUSE_SMOKE=1 .venv/bin/python -m pytest -q -s tests/test_langfuse_live_smoke.py"""

import json
import os

import pytest
from test_intake_validation import RULES
from test_specialized_agent_integration import TWO, Router, two_args
from test_supervisor_delegation import context
from test_supervisor_observability import IDENTIFIERS, assert_safe
from test_supervisor_parallel import FailOne

from rfq_copilot import observability as obs
from rfq_copilot import supervisor as sv
from rfq_copilot.mcp.client import McpToolClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("RFQ_LANGFUSE_SMOKE") != "1", reason="opt-in: RFQ_LANGFUSE_SMOKE=1"
    ),  # fmt: skip
]


class Tee:
    def __init__(self, *sinks):
        self.sinks = sinks

    def record(self, event):
        for sink in self.sinks:
            sink.record(event)


async def test_live_langfuse_synthetic_success_and_controlled_failure(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("no MCP in the smoke test")

    monkeypatch.setattr(McpToolClient, "call", forbidden)
    original = sv._manufacturing_trusted
    monkeypatch.setattr(sv, "_manufacturing_trusted", lambda r: r is RULES or original(r))
    live, reason = obs.from_env()
    assert live is not None, f"Langfuse not enabled: {reason}"  # a fixed reason, no values
    local = obs.MemorySink()
    sink = Tee(live, local)
    ok = await sv.run_supervisor_workflow(context(telemetry=sink))
    failing = Router(manufacturing=FailOne("BRK-201"))
    failed = await sv.run_supervisor_workflow(context(TWO, two_args(), router=failing, telemetry=sink))  # fmt: skip
    live.flush()
    live.shutdown()
    assert_safe(local, *IDENTIFIERS)
    for label, result in (("success", ok), ("controlled_failure", failed)):
        runs = {e.run for e in local.events if e.attempt == result.attempt_id and e.run}
        print(f"\n{label}: attempt={result.attempt_id} run={sorted(runs)} disposition={result.disposition.category}")  # fmt: skip
    print("\nexported metadata:\n" + "\n".join(json.dumps(e.metadata()) for e in local.events))
