"""Checkpoint A4: the least-privilege drafter that turns a ready run into an internal draft.

    python -m rfq_copilot.workflow_drafter --target development --continuous
    python -m rfq_copilot.workflow_drafter --once

A ``completed`` / ``ready_for_draft`` run (Checkpoint A3) is only a trigger, never evidence:
the worker role could set it on any run. The draft is recomputed from the stored RFQ by the
existing ``quote_drafts.create_quote_draft`` (``services.evaluation.evaluate_stored_rfq``
under the server-pinned rules, server-dated horizon). Nothing a model or agent produced is
read; no MCP credential or model is used here.

1. **Claim** (``claim``, one short transaction, ``FOR UPDATE SKIP LOCKED``): a ready run, or a
   ``drafting`` run whose lease expired (crash recovery), becomes ``drafting`` with this
   drafter as ``lease_owner``, a lease deadline, ``draft_attempt_count + 1`` and
   ``claim_generation + 1`` (the fencing token). Expired drafting runs with no attempt left
   are failed first (``draft_lease_expired``).
2. **Draft** (``draft``, ONE transaction): the fenced statement re-checks owner, generation
   and ``drafting``, renews the lease and row-locks the run until commit (a reclaim skips a
   locked row). The service principal is re-resolved from the database
   (``workflow_worker.service_principal``: the one active service identity bound to the
   run's customer, holding ``create_quote_draft`` and no decision permission). Then
   ``create_quote_draft`` with ``idempotency_key = wf-<workflow_run_id>``, the run's
   settlement ``completed`` / ``draft_created`` with the ``quote_id``, and one
   ``workflow.draft`` audit event commit together, or nothing does.
3. **Refusals** settle in a separate fenced transaction after the draft transaction rolled
   back, so a refusal can never leave a quotation behind:
   ``not_draftable`` / ``evaluation_changed`` / ``invalid_evaluation`` / ``quote_exists`` ->
   ``blocked`` / ``draft_refused`` (a quote made under another key is never linked); every
   other ``DraftRefused`` code, an unavailable identity, binding or permission and untrusted
   rules -> ``blocked`` / ``configuration_error``; database faults -> back to ``completed`` /
   ``ready_for_draft`` until ``MAX_DRAFT_ATTEMPTS``, then ``failed`` / ``technical_failure``.

Stale drafters write nothing: every update is conditional on owner + generation +
``drafting``. Fixed codes only; no amount, cost, evidence or exception text is stored or
logged. Nothing here submits, approves, rejects, revises or releases a quotation, and the
customer sees a quotation only after a human release (``api/routers/quotes.py``).
"""

import argparse
import asyncio
import logging
import os
import secrets
import socket
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from rfq_copilot import background
from rfq_copilot import workflow_queue as wq
from rfq_copilot.models import AuditEvent
from rfq_copilot.quote_drafts import DraftRefused, create_quote_draft
from rfq_copilot.workflow_worker import Unavailable, service_principal

logger = logging.getLogger(__name__)

MAX_DRAFT_ATTEMPTS = 3  # ck_workflow_runs_rule_08
DRAFT_PERMISSIONS = frozenset({"create_quote_draft"})  # rbac-policy-v4, rfq_workflow_agent
# Business refusals of the existing service: a human decides; never retried, never linked.
REFUSED = frozenset({"not_draftable", "evaluation_changed", "invalid_evaluation", "quote_exists"})


def _worker_id() -> str:
    return f"drafter-{socket.gethostname()[:32]}-{os.getpid()}-{secrets.token_hex(4)}"


@dataclass(frozen=True)
class DrafterSettings:
    worker_id: str = field(default_factory=_worker_id)
    concurrency: int = 1
    lease_seconds: float = 120.0
    poll_seconds: float = 5.0
    shutdown_grace_seconds: float = 30.0
    error_backoff_max_seconds: float = 60.0

    def __post_init__(self) -> None:
        background.validate_settings(self)
        if not 1 <= self.concurrency <= 4:
            raise ValueError("concurrency must be 1..4")
        if not 0 < self.lease_seconds <= 900:
            raise ValueError("lease_seconds must be in (0, 900]")
        if not 0 < len(self.worker_id) <= 128:
            raise ValueError("worker_id must be 1..128 characters")


@dataclass(frozen=True)
class DraftClaim:
    workflow_run_id: Any
    rfq_id: str
    customer_id: str
    claim_generation: int
    draft_attempt_count: int


_RUNNABLE = (
    "q.draft_attempt_count < :max AND ((q.status = 'completed' AND q.disposition = 'ready_for_draft') "
    "OR (q.status = 'drafting' AND q.lease_expires_at < now()))"
)
_CLAIM = text(
    "UPDATE orchestration.workflow_runs r SET status = 'drafting', disposition = NULL, "
    "lease_owner = :owner, lease_expires_at = now() + make_interval(secs => :lease), "
    "draft_attempt_count = r.draft_attempt_count + 1, claim_generation = r.claim_generation + 1, "
    "updated_at = now() "
    "WHERE r.workflow_run_id = (SELECT q.workflow_run_id FROM orchestration.workflow_runs q "
    f"WHERE {_RUNNABLE} ORDER BY q.created_at, q.workflow_run_id FOR UPDATE SKIP LOCKED LIMIT 1) "
    "RETURNING r.workflow_run_id, r.rfq_id, r.customer_id, r.claim_generation, r.draft_attempt_count"
)
_EXHAUSTED = text(
    "UPDATE orchestration.workflow_runs SET status = 'failed', disposition = 'technical_failure', "
    "last_error = 'draft_lease_expired', lease_owner = NULL, lease_expires_at = NULL, "
    "updated_at = now() WHERE workflow_run_id IN (SELECT q.workflow_run_id FROM "
    "orchestration.workflow_runs q WHERE q.status = 'drafting' AND q.lease_expires_at < now() "
    "AND q.draft_attempt_count >= :max FOR UPDATE SKIP LOCKED) "
    "RETURNING workflow_run_id, rfq_id, customer_id, claim_generation, draft_attempt_count"
)
_OWNED = (
    "workflow_run_id = :run AND claim_generation = :generation AND lease_owner = :owner "
    "AND status = 'drafting'"
)
# The fence: renews the lease and row-locks the run for the whole draft transaction.
_FENCE = text(
    "UPDATE orchestration.workflow_runs SET lease_expires_at = now() + make_interval(secs => :lease), "
    f"updated_at = now() WHERE {_OWNED} RETURNING rfq_id, customer_id"
)
_SETTLE = text(
    "UPDATE orchestration.workflow_runs SET status = :status, disposition = :disposition, "
    "last_error = :error, quote_id = :quote, lease_owner = NULL, lease_expires_at = NULL, "
    "draft_attempt_count = draft_attempt_count - :refund, updated_at = now() "
    f"WHERE {_OWNED}"
)


def _event(run: DraftClaim, actor, status, disposition, error, quote_id, attempt) -> AuditEvent:
    failure = status == "failed" or disposition == "configuration_error" or error is not None and status == "completed"  # fmt: skip
    return AuditEvent(
        actor_id=actor,
        customer_id=run.customer_id,
        rfq_id=run.rfq_id,
        operation=wq.DRAFT_OPERATION,
        outcome="error" if failure else "success",
        correlation_id=run.rfq_id,
        event_data={
            "workflow_run_id": str(run.workflow_run_id),
            "draft_attempt": attempt,
            "claim_generation": run.claim_generation,
            "status": status,
            "disposition": disposition,
            "error": error,
            "quote_id": quote_id,
        },
    )


def _owned(settings: DrafterSettings, run: DraftClaim) -> dict:
    return {"run": run.workflow_run_id, "generation": run.claim_generation, "owner": settings.worker_id}  # fmt: skip


async def claim(database, settings: DrafterSettings) -> DraftClaim | None:
    """Fail exhausted expired drafting leases, then claim one runnable run (or None)."""
    async with database.session() as session, session.begin():
        for row in (await session.execute(_EXHAUSTED, {"max": MAX_DRAFT_ATTEMPTS})).all():
            expired = DraftClaim(*row)
            session.add(_event(expired, None, "failed", "technical_failure", "draft_lease_expired", None, expired.draft_attempt_count))  # fmt: skip
        params = {"max": MAX_DRAFT_ATTEMPTS, "owner": settings.worker_id, "lease": settings.lease_seconds}  # fmt: skip
        row = (await session.execute(_CLAIM, params)).first()
        return DraftClaim(*row) if row else None


async def _settle(session, settings, run, actor, status, disposition, error, quote_id=None, refund=0) -> bool:  # fmt: skip
    changed = await session.execute(
        _SETTLE,
        {"status": status, "disposition": disposition, "error": error, "quote": quote_id,
         "refund": refund, **_owned(settings, run)},
    )  # fmt: skip
    if changed.rowcount != 1:
        return False
    session.add(_event(run, actor, status, disposition, error, quote_id, run.draft_attempt_count - refund))  # fmt: skip
    return True


async def settle(database, settings, run, actor, status, disposition=None, error=None, refund=0) -> bool:  # fmt: skip
    """A fenced settlement in its own transaction (refusals, retries, release); False if
    another drafter now owns the run. Never links a quote."""
    async with database.session() as session, session.begin():
        return await _settle(session, settings, run, actor, status, disposition, error, None, refund)  # fmt: skip


async def draft(database, settings: DrafterSettings, run: DraftClaim, actor: list) -> str:
    """The one draft transaction: fence, identity, create_quote_draft, settlement, commit.
    Raises (rolling everything back) on any refusal; returns ``stale`` if fenced out."""
    async with database.session() as session, session.begin():
        fenced = (await session.execute(_FENCE, {"lease": settings.lease_seconds, **_owned(settings, run)})).first()  # fmt: skip
        if fenced is None or tuple(fenced) != (run.rfq_id, run.customer_id):
            return "stale"  # reclaimed by another drafter (newer generation): write nothing
        principal = await service_principal(session, run.customer_id, DRAFT_PERMISSIONS)
        actor.append(principal.user_id)
        result = await create_quote_draft(
            session,
            principal,
            run.rfq_id,
            idempotency_key=wq.draft_key(run.workflow_run_id),
            correlation_id=run.rfq_id,
            horizon_start=None,  # 54C: the server dates the evaluation context
        )
        if (result.rfq_id, result.customer_id, result.approved, result.released) != (run.rfq_id, run.customer_id, False, False):  # fmt: skip
            raise RuntimeError("draft_inconsistent")  # never commit a mismatched link
        if not await _settle(session, settings, run, principal.user_id, "completed", "draft_created", None, result.quote_id):  # fmt: skip
            raise RuntimeError("fence_lost")  # impossible under the row lock: roll back
        return "completed"


async def process(database, settings: DrafterSettings, run: DraftClaim) -> str:
    """Draft one claimed run to a fenced settlement; returns the settled status or 'stale'."""
    from rfq_copilot.database import DatabaseError, PrivilegedRuntimeRole
    from rfq_copilot.repositories.queries import RuleIntegrityError

    actor: list = []
    try:
        return await draft(database, settings, run, actor)
    except Unavailable as refused:  # identity, binding or permission: nothing was written
        return await _final(database, settings, run, None, "configuration_error", refused.code)
    except DraftRefused as refused:
        who = actor[0] if actor else None
        if refused.reason in REFUSED:
            return await _final(database, settings, run, who, "draft_refused", refused.reason)
        return await _final(database, settings, run, who, "configuration_error", refused.reason)
    except RuleIntegrityError:  # a pinned rule missing or altered
        return await _final(database, settings, run, actor[0] if actor else None, "configuration_error", "rules_untrusted")  # fmt: skip
    except PrivilegedRuntimeRole:
        raise
    except DatabaseError:  # transient: the draft transaction rolled back completely
        return await retry(database, settings, run, "database_unavailable")


async def _final(database, settings, run, actor, disposition: str, code: str) -> str:
    ok = await settle(database, settings, run, actor, "blocked", disposition, code)
    return "blocked" if ok else "stale"


async def retry(database, settings, run: DraftClaim, code: str) -> str:
    """A technical fault: back to ready (bounded by MAX_DRAFT_ATTEMPTS), then failed."""
    if run.draft_attempt_count < MAX_DRAFT_ATTEMPTS:
        ok = await settle(database, settings, run, None, "completed", "ready_for_draft", code)
        return "completed" if ok else "stale"
    ok = await settle(database, settings, run, None, "failed", "technical_failure", code)
    return "failed" if ok else "stale"


async def release(database, settings, run: DraftClaim) -> bool:
    """Graceful shutdown: hand the run back without spending its drafting attempt."""
    return await settle(database, settings, run, None, "completed", "ready_for_draft", "drafter_shutdown", refund=1)  # fmt: skip


class Drafter:
    def __init__(self, database, settings: DrafterSettings) -> None:
        self.database, self.settings = database, settings
        self.stopping = asyncio.Event()
        self._running: dict[asyncio.Task, DraftClaim] = {}

    async def run_once(self) -> int:
        return await background.run_once(self, claim)

    async def _track(self, run: DraftClaim) -> None:
        from rfq_copilot.database import DatabaseError, PrivilegedRuntimeRole

        task = asyncio.create_task(process(self.database, self.settings, run))
        self._running[task] = run
        try:
            status = await task
            logger.info("event=draft_settled status=%s", status)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(release(self.database, self.settings, run))
            except DatabaseError:
                logger.warning("event=draft_release_unavailable recovery=lease")
            if not self.stopping.is_set():
                raise
        except PrivilegedRuntimeRole:
            raise
        except DatabaseError:
            # process already attempted a fenced retry; leave the original lease intact
            # if that settlement also failed. serve observes this error and backs off.
            raise
        except Exception as error:  # a defect: never crash the loop, never leak its text
            logger.error("event=draft_processing_error error_type=%s", type(error).__name__)
            await asyncio.shield(retry(self.database, self.settings, run, "internal_error"))
        finally:
            self._running.pop(task, None)

    async def serve(self) -> None:
        await background.serve(self, claim, logger)


def main(argv=None) -> int:
    from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
    from rfq_copilot.database import Database, DatabaseError

    parser = argparse.ArgumentParser(description="Run the internal quotation drafter.")
    args = background.arguments(parser, os.environ, "RFQ_DRAFTER", argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")  # fmt: skip
    settings = background.settings_from_args(
        parser, args, os.environ, "RFQ_DRAFTER", DrafterSettings
    )

    async def run() -> None:
        config = DatabaseConfig.from_serving_env(args.target, drafter=True)
        async with Database(config) as database:
            drafter = Drafter(database, settings)
            await background.run(drafter, once=args.once)

    try:
        asyncio.run(run())
    except (DatabaseConfigurationError, ValueError, KeyError):
        logger.error("event=drafter_configuration_invalid")
        return 2
    except DatabaseError:
        logger.error("event=drafter_database_refused")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
