"""Checkpoint A3: the trusted background worker that runs the Supervisor for queued RFQs.

    python -m rfq_copilot.workflow_worker --target development --continuous
    python -m rfq_copilot.workflow_worker --once

Customers never reach this: no route, no progress, no evidence. For each claimed run:

1. **Claim** (``claim``): one short transaction, ``FOR UPDATE SKIP LOCKED``. A ``queued``
   run, or a ``running`` run whose lease expired (crash recovery), becomes ``running`` with
   this worker as ``lease_owner``, a lease deadline, ``attempt_count + 1`` and
   ``claim_generation + 1`` (the fencing token). Expired runs with no attempt left are
   failed first (``lease_expired``). ``attempt_count`` never exceeds ``MAX_ATTEMPTS``.
2. **Heartbeat**: every ``heartbeat_seconds`` the lease is renewed *only if* owner and
   generation still match; otherwise the run is cancelled and its result discarded.
3. **Revalidate** (``prepare``), from trusted database state only: the RFQ (customer and
   portal source, read with the run's customer), exactly one active ``rfq_workflow_agent``
   bound to that customer and nothing else, its grants (the three MCP tool permissions,
   never approve/reject/release), and the pinned rules (sha256-checked). The customer's
   JWT is never used: the MCP credential is minted for the service subject.
4. **Run** the existing ``supervisor.run_supervisor_workflow`` with a server-built
   ``SupervisorContext``. Portal specifications without notes enter A1 as structured facts;
   requests with notes retain text extraction. Every tool call passes ``SourceGate``: any
   specification that names a part not in the stored RFQ, or contradicts
   a stored value, is refused and the run is ``blocked`` / ``source_mismatch`` (never
   retried, never priced).
5. **Settle** (``settle``): one conditional UPDATE (owner + generation + ``running``) and
   one ``workflow.attempt`` audit event with codes only. Business outcomes are final
   (clarification -> ``waiting_human``, unsupported -> ``blocked``, draft-ready ->
   ``completed`` / ``ready_for_draft`` for Checkpoint A4); technical failures are retried
   until ``MAX_ATTEMPTS`` then ``failed``; configuration faults are ``blocked`` at once; a
   request whose complete source exceeds the A1 input limit is ``blocked`` /
   ``not_evaluated`` (``source_too_long``), never truncated and never retried.

Nothing here creates, approves, rejects or releases a quotation; the RFQ row is never
written. No agent reasoning, customer text, MCP evidence, amount or credential is stored or
logged. Attempt audit data uses ``workflow_queue.ATTEMPT_KEYS``; source mismatch logs use
only static field/category codes, once per attempt. Each claim, heartbeat,
preparation and settlement opens its own session; nothing is shared between tasks.
"""

import argparse
import asyncio
import logging
import os
import re
import secrets
import socket
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import raiseload

from rfq_copilot import background
from rfq_copilot import intake as a1
from rfq_copilot import supervisor as sv
from rfq_copilot import workflow_queue as wq
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.schemas.rfq import MAX_RAW_REQUEST_CHARS, PartSpecification
from rfq_copilot.mcp.client import McpCallError
from rfq_copilot.models import AuditEvent, Finish, Material, User, UserCustomerBinding
from rfq_copilot.quote_evidence import server_horizon

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = wq.MAX_ATTEMPTS
TOOL_PERMISSIONS = frozenset({"view_catalog", "view_assigned_rfqs", "view_quote"})
FORBIDDEN_PERMISSIONS = frozenset(
    {"approve_quote", "reject_quote", "release_quote", "request_approval", "edit_quote_draft"}
)
_CODE = re.compile(r"^[a-z_]{1,64}$")
# Supervisor codes that mean the deployment is wrong, not that the attempt was unlucky.
_CONFIGURATION = frozenset(
    {
        "untrusted_context", "unauthenticated", "missing_dependency", "invalid_identity",
        "invalid_budget", "manufacturing_rules_untrusted", "pricing_rules_untrusted",
        "approval_rules_untrusted", "forbidden",
    }
)  # fmt: skip
_CATEGORY = {  # escalation.Category -> (status, disposition)
    "clarification_required": ("waiting_human", "clarification_required"),
    "review_required": ("waiting_human", "review_required"),
    "not_quotable": ("blocked", "not_quotable"),
    "not_evaluated": ("blocked", "not_evaluated"),
    "draft_ready_for_approval": ("completed", "ready_for_draft"),
}


class Unavailable(Exception):
    """A fixed-code refusal before the Supervisor runs; ``retry`` marks a transient fault.
    A final refusal settles ``blocked`` with ``disposition`` (a deployment fault by default;
    ``not_evaluated`` when the request itself cannot be evaluated as stored)."""

    def __init__(self, code: str, retry: bool = False, disposition: str = "configuration_error") -> None:  # fmt: skip
        super().__init__(code)
        self.code, self.retry, self.disposition = code, retry, disposition


@dataclass(frozen=True)
class WorkerSettings:
    worker_id: str = field(
        default_factory=lambda: f"{socket.gethostname()[:40]}-{os.getpid()}-{secrets.token_hex(4)}"
    )
    concurrency: int = 1  # ponytail: local hardware default; Ollama serialises anyway
    lease_seconds: float = 120.0
    heartbeat_seconds: float = 30.0
    poll_seconds: float = 5.0
    shutdown_grace_seconds: float = 30.0
    run_timeout_seconds: float = sv.DEFAULT_TIMEOUT
    part_concurrency: int = 2
    error_backoff_max_seconds: float = 60.0

    def __post_init__(self) -> None:
        background.validate_settings(self)
        if not 1 <= self.concurrency <= 4:
            raise ValueError("concurrency must be 1..4")
        if not 0 < self.heartbeat_seconds * 2 < self.lease_seconds <= 900:
            raise ValueError("the lease must outlive two heartbeats (and be at most 900 s)")
        if not 0 < len(self.worker_id) <= 128:
            raise ValueError("worker_id must be 1..128 characters")


@dataclass(frozen=True)
class Claim:
    workflow_run_id: Any
    rfq_id: str
    customer_id: str
    claim_generation: int
    attempt_count: int


# --- queue transitions (each its own short transaction) -----------------------------------

_CLAIM = text(
    "UPDATE orchestration.workflow_runs r SET status = 'running', lease_owner = :owner, "
    "lease_expires_at = now() + make_interval(secs => :lease), "
    "attempt_count = r.attempt_count + 1, claim_generation = r.claim_generation + 1, "
    "updated_at = now() "
    "WHERE r.workflow_run_id = (SELECT q.workflow_run_id FROM orchestration.workflow_runs q "
    "WHERE q.attempt_count < :max AND (q.status = 'queued' OR "
    "(q.status = 'running' AND q.lease_expires_at < now())) "
    "ORDER BY q.created_at, q.workflow_run_id FOR UPDATE SKIP LOCKED LIMIT 1) "
    "RETURNING r.workflow_run_id, r.rfq_id, r.customer_id, r.claim_generation, r.attempt_count"
)
_EXHAUSTED = text(
    "UPDATE orchestration.workflow_runs SET status = 'failed', disposition = 'technical_failure', "
    "last_error = 'lease_expired', lease_owner = NULL, lease_expires_at = NULL, updated_at = now() "
    "WHERE workflow_run_id IN (SELECT q.workflow_run_id FROM orchestration.workflow_runs q "
    "WHERE q.status = 'running' AND q.lease_expires_at < now() AND q.attempt_count >= :max "
    "FOR UPDATE SKIP LOCKED) "
    "RETURNING workflow_run_id, rfq_id, customer_id, claim_generation, attempt_count"
)
_HEARTBEAT = text(
    "UPDATE orchestration.workflow_runs SET lease_expires_at = now() + make_interval(secs => :lease), "
    "updated_at = now() WHERE workflow_run_id = :run AND claim_generation = :generation "
    "AND lease_owner = :owner AND status = 'running'"
)
_SETTLE = text(
    "UPDATE orchestration.workflow_runs SET status = :status, disposition = :disposition, "
    "last_error = :error, lease_owner = NULL, lease_expires_at = NULL, "
    "attempt_count = attempt_count - :refund, "
    "external_workflow_id = COALESCE(:external, external_workflow_id), updated_at = now() "
    "WHERE workflow_run_id = :run AND claim_generation = :generation "
    "AND lease_owner = :owner AND status = 'running'"
)


def _attempt_event(
    claim: Claim, actor: str | None, status, disposition, error, attempt, clarification_codes=()
) -> AuditEvent:
    codes = wq.safe_clarification_codes(clarification_codes) if disposition == "clarification_required" else []  # fmt: skip
    return AuditEvent(
        actor_id=actor,
        customer_id=claim.customer_id,
        rfq_id=claim.rfq_id,
        operation=wq.ATTEMPT_OPERATION,
        outcome="error"
        if status in ("queued", "failed") or disposition == "configuration_error"
        else "success",  # fmt: skip
        correlation_id=claim.rfq_id,
        event_data={
            "workflow_run_id": str(claim.workflow_run_id),
            "attempt": attempt,
            "claim_generation": claim.claim_generation,
            "status": status,
            "disposition": disposition,
            "error": error,
            **({wq.CLARIFICATION_KEY: codes} if codes else {}),
        },
    )


async def claim(database, settings: WorkerSettings) -> Claim | None:
    """Fail exhausted expired leases, then claim one runnable run (or None)."""
    async with database.session() as session, session.begin():
        params = {"max": MAX_ATTEMPTS}
        for row in (await session.execute(_EXHAUSTED, params)).all():
            expired = Claim(*row)
            session.add(_attempt_event(expired, None, "failed", "technical_failure", "lease_expired", expired.attempt_count))  # fmt: skip
        row = (await session.execute(_CLAIM, {**params, "owner": settings.worker_id, "lease": settings.lease_seconds})).first()  # fmt: skip
        return Claim(*row) if row else None


async def heartbeat(database, settings: WorkerSettings, run: Claim) -> bool:
    async with database.session() as session, session.begin():
        renewed = await session.execute(
            _HEARTBEAT,
            {"lease": settings.lease_seconds, "run": run.workflow_run_id,
             "generation": run.claim_generation, "owner": settings.worker_id},
        )  # fmt: skip
        return renewed.rowcount == 1


async def settle(database, settings, run: Claim, actor, status, disposition=None, error=None, external=None, refund=0, clarification_codes=()) -> bool:  # fmt: skip
    """The fenced terminal (or retry/release) update plus its audit event; False if stale."""
    async with database.session() as session, session.begin():
        changed = await session.execute(
            _SETTLE,
            {"status": status, "disposition": disposition, "error": error, "refund": refund,
             "external": external, "run": run.workflow_run_id, "generation": run.claim_generation,
             "owner": settings.worker_id},
        )  # fmt: skip
        if changed.rowcount != 1:
            return False  # reclaimed by another worker (newer generation): discard
        session.add(_attempt_event(run, actor, status, disposition, error, run.attempt_count - refund, clarification_codes))  # fmt: skip
        return True


# --- trusted inputs --------------------------------------------------------------------------

_FIELDS = (
    ("quantity", "quantity", ""),
    ("part_type", "part type", ""),
    ("material_id", "material", ""),
    ("finish_id", "finish", ""),
    ("length_mm", "length", " mm"),
    ("width_mm", "width", " mm"),
    ("height_mm", "height", " mm"),
    ("thickness_mm", "thickness", " mm"),
    ("tolerance_mm", "tolerance", " mm"),
    ("bend_radius_mm", "bend radius", " mm"),
    ("weld_length_mm", "weld length", " mm"),
    ("hole_count", "hole count", ""),
)
# Log vocabulary only; never derive diagnostic names from request data or schema extensions.
_SOURCE_MISMATCH_FIELDS = frozenset(
    {
        "part_id", "quantity", "part_type", "material_id", "finish_id", "length_mm",
        "width_mm", "height_mm", "thickness_mm", "tolerance_mm", "bend_radius_mm",
        "weld_length_mm", "hole_count", "specification",
    }
)  # fmt: skip


def source_text(lines, raw_request: str | None, materials=None, finishes=None) -> str:
    """The deterministic request text A1 reads: every stored line (catalog ids rendered as
    their catalog wording), then the customer's own notes verbatim. A1 still verifies every
    value against exactly this text; the SourceGate checks what it derived from it."""
    names = {"material_id": materials or {}, "finish_id": finishes or {}}
    rendered = []
    for line in lines:
        values = [
            f"{label} {names.get(name, {}).get(getattr(line, name)) or _plain(getattr(line, name))}{unit}"
            for name, label, unit in _FIELDS
            if getattr(line, name) is not None
        ]
        rendered.append(f"Part {line.part_id}: " + ", ".join(values) + ".")
    if raw_request:
        rendered.append(f"Notes: {raw_request}")
    return "\n".join(rendered)


def _plain(value) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value).replace("_", " ")


class SourceGate:
    """Wraps the tool executor: every ``PartSpecification`` an agent sends must name a stored
    part and agree with every value the stored line states. A contradiction or invented part
    is refused (``invalid_input``) before the tool runs and remembered as ``mismatch``.
    Only the first refusal per gate (one workflow attempt) emits a fixed-vocabulary log."""

    def __init__(self, execute: Callable, lines) -> None:
        self._execute, self.mismatch = execute, False
        self._stored = {line.part_id: line for line in lines}

    async def __call__(self, tool: str, request: BaseModel) -> BaseModel:
        for spec in _specs(request):
            line = self._stored.get(spec.part_id)
            conflict = "part_id" if line is None else next(
                (
                    name
                    for name, value in spec.model_dump().items()
                    if name != "part_id"
                    and value is not None
                    and getattr(line, name) is not None
                    and value != getattr(line, name)
                ),
                None,
            )  # fmt: skip
            if conflict is not None:
                first_mismatch = not self.mismatch
                self.mismatch = True
                if first_mismatch:
                    logger.warning(
                        "event=workflow_source_mismatch field=%s category=%s",
                        conflict if conflict in _SOURCE_MISMATCH_FIELDS else "specification",
                        "unknown_part_id" if line is None else "value_conflict",
                    )
                raise McpCallError("invalid_input")
        return await self._execute(tool, request)


def _specs(request: BaseModel) -> list[PartSpecification]:
    part, parts = getattr(request, "part", None), getattr(request, "parts", None)
    return ([part] if isinstance(part, PartSpecification) else []) + list(parts or ())


@dataclass
class Prepared:
    principal: Principal = field(repr=False)
    lines: tuple
    text: str = field(repr=False)
    rules: dict = field(repr=False)
    structured_source: a1.StructuredIntakeSource | None = field(default=None, repr=False)


async def prepare(database, run: Claim) -> Prepared:
    """Revalidate the RFQ, the service identity and the pinned rules from the database."""
    from rfq_copilot.mcp.runtime import pinned_rules
    from rfq_copilot.repositories import get_rfq
    from rfq_copilot.repositories.queries import RuleIntegrityError

    async with database.session() as session, session.begin():
        details = await get_rfq(session, run.rfq_id, customer_id=run.customer_id)
        if details is None or details.rfq.source_type != wq.PORTAL_SOURCE:
            raise Unavailable("rfq_unavailable")
        lines = tuple(details.runtime_lines)
        materials = {m: f"{c} {g}".replace("_", " ") for m, c, g in (await session.execute(select(Material.material_id, Material.category, Material.grade).where(Material.material_id.in_({ln.material_id for ln in lines})))).all()}  # fmt: skip
        finishes = {f: n.lower() for f, n in (await session.execute(select(Finish.finish_id, Finish.display_name).where(Finish.finish_id.in_({ln.finish_id for ln in lines})))).all()}  # fmt: skip
        if not lines:
            raise Unavailable("rfq_unavailable")
        structured_source = None
        if details.rfq.raw_request is None:
            # Only current, customer-scoped portal persistence with no free-text
            # requirements is authoritative structured input. Notes keep the existing
            # extraction/contradiction/ownership path, with the same strict SourceGate.
            try:
                structured_source = a1.StructuredIntakeSource(
                    specifications=tuple(
                        PartSpecification(
                            **{name: getattr(line, name) for name in PartSpecification.model_fields}
                        )
                        for line in lines
                    ),
                    material_names=tuple(materials.items()),
                    finish_names=tuple(finishes.items()),
                )
            except ValidationError:
                raise Unavailable(
                    "invalid_structured_source", disposition="not_evaluated"
                ) from None
            text_ = structured_source.render()[0]
        else:
            text_ = source_text(lines, details.rfq.raw_request, materials, finishes)
        if len(text_) > MAX_RAW_REQUEST_CHARS:
            # D1: every stored line plus the complete notes exceed what A1 may read. Never
            # truncate (a dropped requirement would be invented by omission): the request is
            # final and not evaluated, for a human; not a fault, so never retried. The RFQ
            # row is unchanged. Same limit as supervisor._admit (``invalid_source``).
            raise Unavailable("source_too_long", disposition="not_evaluated")
        principal = await service_principal(session, run.customer_id, TOOL_PERMISSIONS)
        try:
            rules = {name: await pinned_rules(session, version) for name, version in wq.RULE_VERSIONS.items()}  # fmt: skip
        except RuleIntegrityError:
            raise Unavailable("rules_untrusted") from None
    return Prepared(principal, lines, text_, rules, structured_source)


async def service_principal(session, customer_id: str, required: frozenset[str]) -> Principal:
    """The one active ``rfq_workflow_agent`` bound to ``customer_id`` (and to nothing else), as
    a Principal, re-read from the database; it must hold ``required`` and no decision
    permission. Shared by the worker (A3) and the quotation drafter (A4)."""
    from rfq_copilot.repositories import customer_binding, granted_permissions

    candidates = (
        await session.scalars(
            select(User)
            .join(UserCustomerBinding, UserCustomerBinding.user_id == User.user_id)
            .where(
                User.role == wq.SERVICE_ROLE,
                User.active.is_(True),
                UserCustomerBinding.customer_id == customer_id,
                UserCustomerBinding.revoked_at.is_(None),
            )
            .options(raiseload("*"))
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(candidates) != 1:
        raise Unavailable("service_identity_unavailable" if not candidates else "service_identity_ambiguous")  # fmt: skip
    user = candidates[0]
    if await customer_binding(session, user) != customer_id:  # exactly one, eligible
        raise Unavailable("service_binding_invalid")
    granted = await granted_permissions(session, user)
    if not required <= granted or granted & FORBIDDEN_PERMISSIONS:
        raise Unavailable("service_permissions_invalid")
    return Principal(customer_id, granted, user_id=user.user_id, subject=user.authentication_subject)  # fmt: skip


def mcp_executor(integration, principal: Principal) -> Callable:
    """Tool execution bound to the service principal through the MCP credential provider.
    There is no REST credential in the worker, so none can be forwarded."""

    async def execute(tool: str, arguments: BaseModel) -> BaseModel:
        return await integration.call(principal, tool, arguments, rest_credential="")

    return execute


def classify(result: sv.SupervisorExecutionResult, mismatch: bool) -> tuple[str, str | None, str | None]:  # fmt: skip
    """(status, disposition, error) for a finished run; status ``retry`` = technical."""
    if mismatch:
        return "blocked", "source_mismatch", "source_mismatch"
    if result.status == "error":
        code = result.error or "internal_error"
        if code in _CONFIGURATION:
            return "blocked", "configuration_error", code
        if code == "invalid_source":
            return "blocked", "not_evaluated", code
        return "retry", None, code
    category = result.disposition.category if result.disposition else "technical_failure"
    if category in _CATEGORY:
        return (*_CATEGORY[category], None)
    reason = result.route.reason if result.route else None
    code = reason if isinstance(reason, str) and _CODE.match(reason) else "technical_failure"
    if code in _CONFIGURATION:
        return "blocked", "configuration_error", code
    return "retry", None, code


# --- one run -----------------------------------------------------------------------------


@dataclass
class Dependencies:
    """Injected per worker: built from configuration in ``main``, fakes in tests."""

    intake_graph: Any = field(repr=False)
    executor: Callable[[Principal], Callable] = field(repr=False)  # principal -> tool executor
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


async def process(database, settings: WorkerSettings, deps: Dependencies, run: Claim) -> str:
    """Run one claimed job to a fenced settlement; returns the settled status (or 'stale')."""
    actor = None
    try:
        prepared = await prepare(database, run)
    except Unavailable as refused:
        return await _settle_failure(database, settings, run, None, refused.code, refused.retry, refused.disposition)  # fmt: skip
    actor = prepared.principal.user_id
    gate = SourceGate(deps.executor(prepared.principal), prepared.lines)
    context = sv.SupervisorContext(
        principal=prepared.principal,
        execute=gate,
        rfq_id=run.rfq_id,
        source_id=run.rfq_id,
        source_text=prepared.text,
        trusted_part_ids=tuple(line.part_id for line in prepared.lines),
        structured_source=prepared.structured_source,
        manufacturing_rules=prepared.rules["manufacturing"],
        pricing_rules=prepared.rules["pricing"],
        approval_rules=prepared.rules["approval"],
        timeout_seconds=settings.run_timeout_seconds,
        intake_graph=deps.intake_graph,
        horizon_start=server_horizon(deps.now()),
        part_concurrency=settings.part_concurrency,
    )
    lost = asyncio.Event()

    async def beat():
        while True:
            await asyncio.sleep(settings.heartbeat_seconds)
            try:
                renewed = await heartbeat(database, settings, run)
            except Exception:  # a transient DB fault: try again; fencing guards the result
                logger.warning("event=heartbeat_unavailable")
                continue
            if not renewed:
                lost.set()
                return

    beating = asyncio.create_task(beat())
    supervised = asyncio.create_task(sv.run_supervisor_workflow(context))
    watcher = asyncio.create_task(lost.wait())
    try:
        done, _ = await asyncio.wait(
            {supervised, watcher},
            timeout=settings.run_timeout_seconds + 30,
            return_when=asyncio.FIRST_COMPLETED,
        )
        watcher.cancel()
        if supervised not in done:
            supervised.cancel()
            await asyncio.gather(supervised, return_exceptions=True)  # custody cleared
            if lost.is_set():
                logger.warning("event=workflow_lease_lost result=discarded")
                return "stale"
            return await _settle_failure(database, settings, run, actor, "worker_timeout", True)
        result = supervised.result()
    except asyncio.CancelledError:
        supervised.cancel()
        raise
    finally:
        beating.cancel()
        watcher.cancel()
        if not supervised.done():
            supervised.cancel()
        await asyncio.gather(beating, watcher, supervised, return_exceptions=True)
    status, disposition, error = classify(result, gate.mismatch)
    if status == "retry":
        return await _settle_failure(database, settings, run, actor, error, True)
    external = result.attempt_id if result.status == "executed" else None
    codes = result.disposition.clarification_codes if result.disposition else ()
    ok = await settle(database, settings, run, actor, status, disposition, error, external, clarification_codes=codes)  # fmt: skip
    return status if ok else "stale"


async def _settle_failure(database, settings, run, actor, code: str, retry: bool, final: str = "configuration_error") -> str:  # fmt: skip
    if not retry:
        status, disposition = "blocked", final
    elif run.attempt_count < MAX_ATTEMPTS:
        status, disposition = "queued", None  # bounded technical retry
    else:
        status, disposition = "failed", "technical_failure"
    ok = await settle(database, settings, run, actor, status, disposition, code)
    return status if ok else "stale"


async def release(database, settings, run: Claim) -> bool:
    """Graceful shutdown: hand the run back without spending its attempt."""
    return await settle(database, settings, run, None, "queued", None, "worker_shutdown", refund=1)


# --- the loop --------------------------------------------------------------------------------


class Worker:
    def __init__(self, database, settings: WorkerSettings, deps: Dependencies) -> None:
        self.database, self.settings, self.deps = database, settings, deps
        self.stopping = asyncio.Event()
        self._running: dict[asyncio.Task, Claim] = {}

    async def run_once(self) -> int:
        """Claim and process up to ``concurrency`` jobs concurrently; returns how many."""
        return await background.run_once(self, claim)

    async def _track(self, run: Claim) -> None:
        from rfq_copilot.database import DatabaseError, PrivilegedRuntimeRole

        task = asyncio.create_task(process(self.database, self.settings, self.deps, run))
        self._running[task] = run
        try:
            status = await task
            logger.info("event=workflow_settled status=%s", status)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(release(self.database, self.settings, run))
            except DatabaseError:
                logger.warning("event=workflow_release_unavailable recovery=lease")
            if not self.stopping.is_set():
                raise
        except PrivilegedRuntimeRole:
            raise
        except DatabaseError:
            await _settle_failure(
                self.database, self.settings, run, None, "database_unavailable", True
            )
        except Exception as error:  # a defect: never crash the loop, never leak its text
            logger.error("event=workflow_processing_error error_type=%s", type(error).__name__)
            await asyncio.shield(_settle_failure(self.database, self.settings, run, None, "internal_error", True))  # fmt: skip
        finally:
            self._running.pop(task, None)

    async def serve(self) -> None:
        """Poll until ``stopping``; then let in-flight runs finish within the grace period and
        release (attempt refunded) whatever is still running."""
        await background.serve(self, claim, logger)


def _intake_graph_from_env(environ, *, resources: AsyncExitStack | None = None):
    from rfq_copilot.intake import build_intake_graph

    provider = environ.get("RFQ_WORKFLOW_LLM", "")
    if provider != "ollama":
        raise SystemExit("RFQ_WORKFLOW_LLM must be 'ollama' (no paid model is ever used).")
    from langchain_ollama import ChatOllama

    model = ChatOllama(
        model=environ["RFQ_WORKFLOW_OLLAMA_MODEL"],
        base_url=environ.get("RFQ_WORKFLOW_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        temperature=0, seed=7, reasoning=False, num_ctx=8192,
    )  # fmt: skip
    if resources is not None:
        # ChatOllama has no public close method. Its owned Ollama clients do; keep
        # this adapter here rather than inspecting the compiled graph at shutdown.
        resources.callback(model._client.close)
        resources.push_async_callback(model._async_client.close)
    return build_intake_graph(model)


def _mcp_from_env(environ):
    from pathlib import Path

    from rfq_copilot.api.mcp_integration import McpIntegration
    from rfq_copilot.dev_token import DevMcpCredentialProvider
    from rfq_copilot.mcp.client import McpToolClient
    from rfq_copilot.mcp.security import McpAuthSettings

    auth = McpAuthSettings.from_env({k: v for k, v in environ.items() if k.startswith("RFQ_MCP_") and not k.startswith("RFQ_MCP_HOST_")})  # fmt: skip
    provider = DevMcpCredentialProvider(
        auth,
        Path(environ["RFQ_WORKFLOW_MCP_SIGNING_KEY_FILE"]),
        environment=environ.get("RFQ_WORKFLOW_ENVIRONMENT", "production"),
    )
    integration = McpIntegration(
        client=McpToolClient(environ["RFQ_WORKFLOW_MCP_BASE_URL"]),
        credentials=provider,
        mcp_audience=auth.jwt_audience,
        rest_audience=environ.get("RFQ_API_JWT_AUDIENCE", ""),
    )
    return lambda principal: mcp_executor(integration, principal)


def main(argv=None) -> int:
    from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
    from rfq_copilot.database import Database, DatabaseError
    from rfq_copilot.dev_token import DevTokenError

    parser = argparse.ArgumentParser(description="Run the RFQ workflow worker.")
    args = background.arguments(parser, os.environ, "RFQ_WORKFLOW", argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")  # fmt: skip
    environ = os.environ
    settings = background.settings_from_args(parser, args, environ, "RFQ_WORKFLOW", WorkerSettings)

    async def run() -> None:
        config = DatabaseConfig.from_serving_env(args.target, workflow=True)
        async with AsyncExitStack() as resources:
            deps = Dependencies(
                intake_graph=_intake_graph_from_env(environ, resources=resources),
                executor=_mcp_from_env(environ),
            )
            async with Database(config) as database:
                worker = Worker(database, settings, deps)
                await background.run(worker, once=args.once)

    try:
        asyncio.run(run())
    except (DatabaseConfigurationError, DevTokenError, ValueError, KeyError, OSError):
        logger.error("event=worker_configuration_invalid")
        return 2
    except DatabaseError:
        logger.error("event=worker_database_refused")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
