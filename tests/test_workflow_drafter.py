"""Checkpoint A4, offline: the drafter's outcome mapping, refusal vocabulary, settings and
entry point. No database: ``draft`` and the fenced ``settle`` are replaced by recorders (the
database behaviour is in test_workflow_drafter_database)."""

import asyncio
import uuid
from typing import get_args

import pytest

from rfq_copilot import quote_drafts
from rfq_copilot import workflow_drafter as wd
from rfq_copilot import workflow_queue as wq
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import DatabaseError
from rfq_copilot.quote_drafts import DraftRefused
from rfq_copilot.repositories.queries import RuleIntegrityError
from rfq_copilot.workflow_worker import Unavailable

RUN = wd.DraftClaim(uuid.uuid4(), "RFQ-" + "a" * 32, "CUST-001", 2, 1)
SETTINGS = wd.DrafterSettings(worker_id="d-test")


@pytest.fixture
def settled(monkeypatch):
    calls = []

    async def settle(database, settings, run, actor, status, disposition=None, error=None, refund=0):  # fmt: skip
        calls.append((actor, status, disposition, error, refund))
        return True

    monkeypatch.setattr(wd, "settle", settle)
    return calls


def raising(error, actor=None):
    async def draft(database, settings, run, who):
        if actor:
            who.append(actor)
        raise error

    return draft


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", sorted(wd.REFUSED))
async def test_business_refusals_are_final_draft_refused(monkeypatch, settled, reason):
    monkeypatch.setattr(wd, "draft", raising(DraftRefused(reason), "U-SVC-A"))
    assert await wd.process(None, SETTINGS, RUN) == "blocked"
    assert settled == [("U-SVC-A", "blocked", "draft_refused", reason, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["permission_denied", "not_found", "invalid_request", "idempotency_key_conflict", "calculation_record_mismatch"])  # fmt: skip
async def test_other_service_refusals_are_configuration_errors(monkeypatch, settled, reason):
    monkeypatch.setattr(wd, "draft", raising(DraftRefused(reason), "U-SVC-A"))
    assert await wd.process(None, SETTINGS, RUN) == "blocked"
    assert settled == [("U-SVC-A", "blocked", "configuration_error", reason, 0)]


def test_every_mapped_code_is_the_existing_service_vocabulary():
    vocabulary = set(get_args(quote_drafts.Reason))
    assert wd.REFUSED <= vocabulary  # never an invented state
    assert wd.REFUSED == {"not_draftable", "evaluation_changed", "invalid_evaluation", "quote_exists"}  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code",
    [
        (Unavailable("service_identity_unavailable"), "service_identity_unavailable"),
        (Unavailable("service_binding_invalid"), "service_binding_invalid"),
        (Unavailable("service_permissions_invalid"), "service_permissions_invalid"),
        (RuleIntegrityError("workflow-v1.2 is not seeded."), "rules_untrusted"),
    ],
)
async def test_identity_and_rule_faults_are_configuration_errors(monkeypatch, settled, error, code):
    monkeypatch.setattr(wd, "draft", raising(error))
    assert await wd.process(None, SETTINGS, RUN) == "blocked"
    assert settled == [(None, "blocked", "configuration_error", code, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt,expected", [(1, ("completed", "ready_for_draft")), (2, ("completed", "ready_for_draft")), (3, ("failed", "technical_failure"))])  # fmt: skip
async def test_database_faults_retry_within_the_attempt_limit(monkeypatch, settled, attempt, expected):  # fmt: skip
    run = wd.DraftClaim(RUN.workflow_run_id, RUN.rfq_id, RUN.customer_id, 1 + attempt, attempt)
    monkeypatch.setattr(wd, "draft", raising(DatabaseError("Database operation failed.")))
    assert await wd.process(None, SETTINGS, run) == expected[0]
    assert settled == [(None, *expected, "database_unavailable", 0)]


@pytest.mark.asyncio
async def test_a_stale_draft_settles_nothing(monkeypatch, settled):
    async def stale(database, settings, run, who):
        return "stale"

    monkeypatch.setattr(wd, "draft", stale)
    assert await wd.process(None, SETTINGS, RUN) == "stale" and settled == []


@pytest.mark.asyncio
async def test_a_defect_propagates_to_the_loop_which_retries_without_its_text(monkeypatch, settled, caplog):  # fmt: skip
    monkeypatch.setattr(wd, "draft", raising(RuntimeError("secret detail in a defect")))
    with pytest.raises(RuntimeError):
        await wd.process(None, SETTINGS, RUN)
    drafter = wd.Drafter(None, SETTINGS)
    await drafter._track(RUN)
    assert settled == [(None, "completed", "ready_for_draft", "internal_error", 0)]
    assert "secret detail" not in caplog.text


@pytest.mark.asyncio
async def test_shutdown_releases_the_run_with_its_attempt_refunded(monkeypatch, settled):
    started = asyncio.Event()

    async def slow(database, settings, run, who):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(wd, "draft", slow)
    drafter = wd.Drafter(None, SETTINGS)
    drafter.stopping.set()
    task = asyncio.create_task(drafter._track(RUN))
    await started.wait()
    next(iter(drafter._running)).cancel()
    await task
    assert settled == [(None, "completed", "ready_for_draft", "drafter_shutdown", 1)]


def test_the_idempotency_key_is_the_immutable_run_id():
    run_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert wq.draft_key(run_id) == "wf-12345678-1234-5678-1234-567812345678"
    assert len(wq.draft_key(run_id)) <= 256 and wq.draft_key(run_id).isascii()


@pytest.mark.parametrize("over", [{"concurrency": 0}, {"concurrency": 5}, {"lease_seconds": 0}, {"lease_seconds": 901}, {"worker_id": ""}])  # fmt: skip
def test_settings_refuse_unsafe_bounds(over):
    with pytest.raises(ValueError):
        wd.DrafterSettings(**over)


def test_the_entry_point_selects_only_the_drafter_credential(monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def from_serving_env(target="development", *, environ=None, decision=False, workflow=False, drafter=False):  # fmt: skip
        seen.update(target=target, decision=decision, workflow=workflow, drafter=drafter)
        raise Stop

    monkeypatch.setattr(DatabaseConfig, "from_serving_env", staticmethod(from_serving_env))
    with pytest.raises(Stop):
        wd.main(["--once"])
    assert seen == {"target": "development", "decision": False, "workflow": False, "drafter": True}


def test_drafter_configuration_reads_only_its_own_credential():
    from test_serving_database_config import SelectedKeysOnly
    from test_workflow_database_config import OTHERS, workflow_environment

    env = workflow_environment() | OTHERS
    env["DRAFTER_DATABASE_URL"] = env.pop("WORKFLOW_DATABASE_URL").replace("synthetic_workflow", "synthetic_drafter")  # fmt: skip
    watched = SelectedKeysOnly(env)
    config = DatabaseConfig.from_serving_env(environ=watched, drafter=True)
    assert (config.drafter, config.runtime, config.workflow, config.decision) == (True, False, False, False)  # fmt: skip
    assert not watched.read & set(OTHERS) and "WORKFLOW_DATABASE_URL" not in watched.read
    with pytest.raises(Exception, match="DRAFTER_DATABASE_URL is required"):
        DatabaseConfig.from_serving_env(environ=SelectedKeysOnly(dict(OTHERS)), drafter=True)
    with pytest.raises(Exception, match="one restricted credential"):
        DatabaseConfig.from_serving_env(environ=env, drafter=True, workflow=True)


@pytest.mark.asyncio
async def test_continuous_drafter_recovers_after_a_temporary_database_failure(monkeypatch, settled):
    drafter = wd.Drafter(None, wd.DrafterSettings(poll_seconds=0.05))
    attempts = []

    async def claim(*args):
        attempts.append(1)
        return wd.DraftClaim(RUN.workflow_run_id, RUN.rfq_id, RUN.customer_id, len(attempts), len(attempts))  # fmt: skip

    async def draft(database, settings, run, actor):
        if len(attempts) == 1:
            raise DatabaseError("temporary fault")
        drafter.stopping.set()
        return "completed"

    monkeypatch.setattr(wd, "claim", claim)
    monkeypatch.setattr(wd, "draft", draft)
    await asyncio.wait_for(drafter.serve(), 1)
    assert len(attempts) == 2
    assert settled == [(None, "completed", "ready_for_draft", "database_unavailable", 0)]
