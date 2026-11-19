"""Checkpoint A1: the durable automatic RFQ processing queue (``orchestration.workflow_runs``).

- ``enqueue``: called by ``rfq_submission`` inside the RFQ's own savepoint, so the RFQ, its
  lines, receipt, creation event, exactly one ``queued`` run and its ``workflow.enqueue``
  event commit or roll back together. ``uq_workflow_runs_active_rfq`` admits one active run
  per RFQ; ``correlation_id`` (unique) is the RFQ id, so an RFQ is enqueued at most once.
- Activation boundary: an RFQ is enqueued only after the owner CLI recorded activation:
  one immutable ``workflow.queue_activate`` audit event, approved by an active
  administrator, written under ``LOCK TABLE business.rfqs IN SHARE MODE`` and listing every
  customer-portal RFQ that existed then (``historical_rfq_ids``). The runtime integrity
  audit verifies the boundary by set membership, not timestamps: listed RFQs have no run,
  every later portal RFQ has exactly one.
- Kill switch: the API enqueues only with ``RFQ_API_WORKFLOW_QUEUE=enabled`` (default
  ``off``: submission is exactly the pre-A1 behaviour). Enabled without the activation
  event, submissions are refused (503) rather than stored without a processing request.

    python -m rfq_copilot.workflow_queue activate --target development --env-file .env \\
        --confirm-database rfq_copilot_dev --approved-by ADMIN [--execute]
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import func, select, text

from rfq_copilot.mcp.contracts import NEW_EVALUATION_APPROVAL_RULE_VERSION
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION, PRICING_RULE_VERSION
from rfq_copilot.models import RFQ, AuditEvent, User, WorkflowRun

GRAPH_VERSION = "supervisor-execution-v1"  # run_supervisor_workflow (delegation)
# Server-side pins the worker loads (sha256-checked); never from the request.
RULE_VERSIONS = {
    "manufacturing": MANUFACTURING_RULE_VERSION,
    "pricing": PRICING_RULE_VERSION,
    "approval": NEW_EVALUATION_APPROVAL_RULE_VERSION,
}
MAX_ATTEMPTS = 3  # ck_workflow_runs_rule_04
ENQUEUE_OPERATION = "workflow.enqueue"
ACTIVATION_OPERATION = "workflow.queue_activate"
ACTIVATION_CORRELATION = "workflow-queue-activation"
PORTAL_SOURCE = "customer_portal"  # rfq_submission.SOURCE_TYPE
APPROVER_ROLE = "administrator"
SERVICE_ROLE = "rfq_workflow_agent"  # rbac-policy-v4 (operator_admin.SERVICE_ROLE)
# One audit event per settled attempt (workflow_worker): codes only, never text or evidence.
ATTEMPT_OPERATION = "workflow.attempt"
ATTEMPT_KEYS = frozenset({"workflow_run_id", "attempt", "claim_generation", "status", "disposition", "error"})  # fmt: skip
# Optional, only on a clarification_required settlement; absent on historical events.
CLARIFICATION_KEY = "clarification_codes"


def safe_clarification_codes(codes) -> list[str]:
    """The distinct structured codes among ``codes`` (invalid ones dropped), bounded."""
    from rfq_copilot import escalation as esc  # local: keeps this module's import light

    kept = dict.fromkeys(c for c in codes if esc.is_clarification_code(c))
    return list(kept)[: esc.MAX_QUESTIONS]


def clarification_codes_valid(value) -> bool:
    """Integrity form: a non-empty bounded list of distinct valid codes, nothing else."""
    return isinstance(value, list) and bool(value) and safe_clarification_codes(value) == value


# Settled status -> the dispositions it may carry (ck_workflow_runs_rule_06 vocabulary).
STATUS_DISPOSITIONS = {
    "queued": {None},  # waiting for a (re)claim; a retry keeps last_error
    "running": {None},
    "drafting": {None},  # Checkpoint A4: claimed by the quotation drafter
    "waiting_human": {"clarification_required", "review_required"},
    "blocked": {
        "not_quotable",
        "not_evaluated",
        "source_mismatch",
        "configuration_error",
        "draft_refused",
    },  # fmt: skip
    "completed": {"ready_for_draft", "draft_created"},  # A4 drafts from ready_for_draft
    "failed": {"technical_failure"},
}
# Checkpoint A4: one audit event per settled drafting attempt (workflow_drafter), codes only.
DRAFT_OPERATION = "workflow.draft"
DRAFT_KEYS = frozenset({"workflow_run_id", "draft_attempt", "claim_generation", "status", "disposition", "error", "quote_id"})  # fmt: skip
DRAFT_KEY_PREFIX = "wf-"  # create_quote_draft idempotency key: wf-<workflow_run_id>


def draft_key(workflow_run_id) -> str:
    return f"{DRAFT_KEY_PREFIX}{workflow_run_id}"


async def queue_activated(session) -> bool:
    return bool(
        await session.scalar(
            select(func.count()).where(
                AuditEvent.correlation_id == ACTIVATION_CORRELATION,
                AuditEvent.operation == ACTIVATION_OPERATION,
                AuditEvent.outcome == "success",
            )
        )
    )


def enqueue(session, *, rfq_id: str, customer_id: str, actor: str) -> uuid.UUID:
    """Add one queued run and its provenance event; the caller's savepoint flushes both."""
    run_id = uuid.uuid4()
    session.add(
        WorkflowRun(
            workflow_run_id=run_id,
            customer_id=customer_id,
            rfq_id=rfq_id,
            initiated_by=actor,
            correlation_id=rfq_id,
            status="queued",
            graph_version=GRAPH_VERSION,
            rule_versions=dict(RULE_VERSIONS),
            source_snapshot_versions={},
        )
    )
    session.add(
        AuditEvent(
            actor_id=actor,
            customer_id=customer_id,
            rfq_id=rfq_id,
            operation=ENQUEUE_OPERATION,
            outcome="success",
            correlation_id=rfq_id,
            event_data={"workflow_run_id": str(run_id)},
        )
    )
    return run_id


class ActivationRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def activate(session, approver: str) -> list[str]:
    """Record the activation boundary; returns the historical portal RFQ ids. No commit."""
    await session.execute(text("LOCK TABLE business.rfqs IN SHARE MODE"))  # no RFQ insert races
    if await queue_activated(session):
        raise ActivationRefused("already_activated")
    approved = await session.scalar(
        select(func.count()).where(
            User.user_id == approver, User.active.is_(True), User.role == APPROVER_ROLE
        )
    )
    if not approved:
        raise ActivationRefused("approval_requires_active_administrator")
    if await session.scalar(select(func.count()).select_from(WorkflowRun)):
        raise ActivationRefused("workflow_runs_exist")
    historical = sorted(
        (await session.scalars(select(RFQ.rfq_id).where(RFQ.source_type == PORTAL_SOURCE))).all()
    )
    session.add(
        AuditEvent(
            actor_id=approver,
            operation=ACTIVATION_OPERATION,
            outcome="success",
            correlation_id=ACTIVATION_CORRELATION,
            event_data={"historical_rfq_ids": historical, "graph_version": GRAPH_VERSION},
        )
    )
    await session.flush()
    return historical


def main(argv=None) -> int:
    from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
    from rfq_copilot.database import Database, DatabaseError
    from rfq_copilot.seeding import SeedError
    from rfq_copilot.seeding.safety import validate_target, verify_schema

    parser = argparse.ArgumentParser(description="Activate the automatic RFQ processing queue.")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("activate")
    command.add_argument("--target", choices=("development", "test"), required=True)
    command.add_argument("--env-file", type=Path)
    command.add_argument("--confirm-database", required=True)
    command.add_argument("--approved-by", required=True)
    command.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    async def run() -> dict:
        config = DatabaseConfig.from_env(args.target, env_file=args.env_file)
        validate_target(config)
        if args.confirm_database != config.url.database:
            raise SeedError("Confirmation does not match the target database.")
        async with Database(config) as database:
            async with database.session() as session:
                await (await session.connection()).run_sync(verify_schema, config)
                historical = await activate(session, args.approved_by)
                if args.execute:
                    await session.commit()
        return {"status": "ok", "historical_rfqs": len(historical),
                "mode": "execute" if args.execute else "dry-run"}  # fmt: skip

    try:
        result = asyncio.run(run())
    except ActivationRefused as refused:
        print(json.dumps({"status": "refused", "error": refused.code}))
        return 2
    except (SeedError, DatabaseConfigurationError, DatabaseError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
