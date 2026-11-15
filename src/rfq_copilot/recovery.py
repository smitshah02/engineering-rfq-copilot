"""Supervisor checkpointing and recovery (Day 49): pause at a stage boundary, continue the same
attempt from its original evidence, restart as a new attempt, retrieve, cancel, refuse.

    SupervisorRecovery.start(context, pause_before=stage) -> RecoveryResult(paused, id, gen)
    SupervisorRecovery.resume(context, id, gen)           -> the same attempt continues
    SupervisorRecovery.restart(context, id)               -> a NEW attempt, fresh evaluation
    SupervisorRecovery.retrieve(principal, id) / cancel(principal, id) / export / adopt

- Continuation is real but in-process: a paused attempt keeps its ``supervisor._Custody`` (the
  original typed A1-A5 results) alive in memory, never serialized. On resume the execution
  graph re-records each held result through ``record_outcome`` (identity, rules, handoffs and
  provenance re-checked under the new context) without calling any agent, and must reproduce
  the outcomes recorded at the pause, else ``stale_recovery``. ``decide()`` then delegates the
  next stage as always. Nothing is reconstructed from metadata.
- What may be persisted is only ``RecoveryRecord``: versioned, size-bounded metadata (owner
  hash, trusted identity, attempt, generation, rule fingerprints, planning horizon, lifecycle,
  next stage, unused budget, expiry). No text, evidence, amounts, credentials or callables.
  A record adopted after a restart has no custody: continuation is refused
  (``evidence_unavailable``); an explicit restart (new attempt) is allowed.
- Ownership: ids are server-chosen (``secrets``); every call re-derives the owner from the
  verified ``Principal`` (``ownership.principal_owner``, as threads) and re-admits the context
  (``supervisor._admit``). An unknown, malformed or foreign id gets one opaque refusal. Resume
  needs the current generation, the same identity (customer, RFQ, source, source sha256), the
  same rule fingerprints and the same planning horizon.
- Concurrency: every lifecycle transition is claimed synchronously under a ``threading.Lock``
  (no await inside), so one resume wins and others get ``attempt_busy``. Guarantees are
  process-local: no cross-worker or cross-restart exclusion, and no exactly-once for tools.
- Cancellation: a request on a running attempt is recorded under the lock and the task is
  cancelled on its own loop (``call_soon_threadsafe``); the response acknowledges the request.
  The attempt then ends ``cancelled`` even if nested code swallows the cancellation.
- Retention: paused custody (internal evidence) is released on access, insertion, ``prune()``
  or ``close()``, not by a timer at the exact TTL. A running entry is never pruned while its
  task may still run; one whose task ended abruptly or whose loop is closed is settled
  (``_reap``, D50-01) and dropped. A pending task on a loop that is stopped but never closed
  cannot be told apart from a slow one and keeps its slot (process-local boundary).
- Retries: none automatic. A restart is the only re-execution, explicit, a new attempt, at
  most ``MAX_RESTARTS`` per lineage. Cancelled, failed, expired or superseded attempts never
  become completed. Approval and release stay ``Literal[False]``.
"""

import asyncio
import functools
import hmac
import inspect
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from rfq_copilot import observability as obs
from rfq_copilot import supervisor as sv
from rfq_copilot.agent_integration import Stage
from rfq_copilot.api.auth import Principal
from rfq_copilot.ownership import ThreadAccessError
from rfq_copilot.ownership import principal_owner as _owner

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 4096
MAX_ENTRIES = 256
DEFAULT_TTL = 900.0  # seconds a paused attempt (or a finished result) is kept
MAX_RESTARTS = 2
STAGES = tuple(s.stage for s in sv.STAGE_PLAN)

Lifecycle = Literal["running", "paused", "completed", "failed", "cancelled", "expired", "superseded"]  # fmt: skip
Outcome = Literal["paused", "completed", "failed", "cancelled", "expired", "recovery_refused"]
Operation = Literal["start", "resume", "restart", "retrieve", "cancel"]
Refusal = Literal[
    "recovery_unavailable",  # unknown, malformed or another principal's id (one answer)
    "attempt_busy",  # running: another call owns it
    "not_resumable",  # completed, failed, cancelled or superseded
    "expired",
    "stale_generation",
    "evidence_unavailable",  # no live custody (e.g. adopted after a process restart)
    "identity_mismatch",
    "source_changed",
    "rules_changed",
    "policy_changed",
    "stale_recovery",
    "restart_limit",
    "recovery_capacity",
    "invalid_record",
]
_Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{32}$")]  # secrets.token_urlsafe(24)
_Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")]  # rule version / fingerprint
_Code = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]  # fixed codes only
_STRICT = ConfigDict(extra="forbid", frozen=True)


class RecoveryRecord(BaseModel):
    """The only persistable recovery data: metadata, never evidence (see module docstring)."""

    model_config = _STRICT
    schema_version: Literal[1] = SCHEMA_VERSION
    recovery_id: _Id
    owner: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]  # ownership.principal_owner
    identity: sv.WorkflowIdentity  # customer (from the principal), RFQ, source, source sha256
    attempt_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    generation: Annotated[StrictInt, Field(ge=0, le=1_000_000)] = 0
    rules: Annotated[tuple[tuple[_Name, _Name], ...], Field(max_length=3)]
    horizon_start: date | None = None
    lifecycle: Lifecycle
    next_stage: Stage | None = None  # paused: the stage a resume runs first
    budget_left_seconds: Annotated[float, Field(ge=0, le=sv.DEFAULT_TIMEOUT)] | None = None
    expires_at: Annotated[float, Field(ge=0)]  # unix seconds
    restarts: Annotated[StrictInt, Field(ge=0, le=MAX_RESTARTS)] = 0
    code: _Code | None = None

    @model_validator(mode="after")
    def _consistent(self):
        paused = self.lifecycle == "paused"
        if paused != (self.next_stage is not None) or (paused and self.budget_left_seconds is None):  # fmt: skip
            raise ValueError("inconsistent recovery record")
        if self.next_stage == "preflight":
            raise ValueError("inconsistent recovery record")
        return self

    def to_json(self) -> str:
        return self.model_dump_json()  # field order fixed by the model: deterministic

    @classmethod
    def from_json(cls, data: str | bytes) -> "RecoveryRecord":
        """Strict, bounded, fail-closed: JSON only (no pickle, no object revival)."""
        raw = data.encode() if isinstance(data, str) else data
        if not isinstance(raw, bytes) or len(raw) > MAX_RECORD_BYTES:
            raise ValueError("invalid recovery record")
        try:
            return cls.model_validate_json(raw)
        except ValidationError:
            raise ValueError("invalid recovery record") from None


class RecoveryResult(BaseModel):
    """The technical outcome of one recovery operation. The business outcome is
    ``result.disposition``; a paused or refused operation concludes nothing about the RFQ."""

    model_config = _STRICT
    operation: Operation
    outcome: Outcome
    recovery_id: str | None = None
    generation: int | None = None
    attempt_id: str | None = None
    next_stage: Stage | None = None
    code: str | None = None
    restarted_from: str | None = None  # restart: the superseded attempt's recovery id
    result: sv.SupervisorExecutionResult | None = None
    human_approval_required: Literal[True] = True
    approved: Literal[False] = False
    released: Literal[False] = False

    @model_validator(mode="after")
    def _consistent(self):
        o = self.outcome
        ok = (
            (o == "paused") == (self.next_stage is not None)
            and (self.result is None or o in ("completed", "failed"))
            and (o != "completed" or (self.result is not None and self.code is None))
            and (o not in ("failed", "recovery_refused", "expired") or self.code is not None)
        )
        if not ok:
            raise ValueError("inconsistent recovery result")
        return self

    def customer_outcome(self):
        """Day 48's customer projection of a finished attempt, or None (nothing concluded)."""
        return self.result.customer_outcome() if self.result is not None else None


@dataclass
class _Entry:
    record: RecoveryRecord
    custody: sv._Custody | None  # live, in-process only; never serialized
    result: sv.SupervisorExecutionResult | None = None
    task: asyncio.Task | None = None
    loop: asyncio.AbstractEventLoop | None = None  # the running task's own loop
    cancel_requested: bool = False  # set under the lock; a later result is never published


# Approved recovery telemetry vocabularies (pre-Phase 8, Task 2). ``RecoveryResult.code`` is a
# free ``str``: it carries refusal codes, Supervisor admission codes, lifecycle codes and, for a
# failed run, the run's error or route reason, so all of those vocabularies are admitted.
_CODES = frozenset(get_args(Refusal)) | sv._TELEMETRY_CODES["error"] | sv._TELEMETRY_CODES["reason"]
_LIFECYCLE = {"cancelled", "cancel_requested", "expired", "superseded", "interrupted", "failed"}
_VOCABULARY: dict[str, frozenset[str]] = {
    "operation": frozenset(get_args(Operation)) | {"reap"},
    "outcome": frozenset(get_args(Outcome)) | frozenset(get_args(Lifecycle)),
    "code": _CODES | _LIFECYCLE,
}


def _refused(operation: Operation, code: str, recovery_id=None) -> RecoveryResult:
    return RecoveryResult(operation=operation, outcome="recovery_refused", code=code, recovery_id=recovery_id)  # fmt: skip


def _reported(method):
    """Day 50: report each operation's RecoveryResult as one ``recovery`` telemetry event
    (operation, outcome, fixed code, generation, attempt id), after the transition is made.
    Never the recovery id (a resumable handle), owner hash, identity or result contents."""
    is_async = inspect.iscoroutinefunction(method)

    async def run(self, *args, **kwargs):
        return self._report(await method(self, *args, **kwargs))

    def call(self, *args, **kwargs):
        return self._report(method(self, *args, **kwargs))

    return functools.wraps(method)(run if is_async else call)


class SupervisorRecovery:
    """Process-local, principal-scoped recovery of Supervisor attempts (development level)."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL, max_entries: int = MAX_ENTRIES, clock=time.time, telemetry=None):  # fmt: skip
        self._ttl, self._max, self._clock = ttl_seconds, max_entries, clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()  # transitions only; never held across an await
        self._telemetry = telemetry  # observability sink for recovery events; write-only

    # -- internals --------------------------------------------------------------------------

    def _event(self, **fields) -> None:
        """One ``recovery`` telemetry event, its codes normalized through the approved
        vocabularies (anything else is exported as ``"unlisted"``)."""
        fields = {k: (v if v is None or k not in _VOCABULARY or v in _VOCABULARY[k] else "unlisted") for k, v in fields.items()}  # fmt: skip
        obs.emit(self._telemetry, "recovery", **fields)

    def _report(self, result: RecoveryResult) -> RecoveryResult:
        self._event(operation=result.operation, outcome=result.outcome, code=result.code, generation=result.generation, attempt=result.attempt_id)  # fmt: skip
        return result

    def _admit(self, context):
        """(owner, admitted) from a trusted context, or a refusal code."""
        try:
            owner = _owner(context.principal)
            return owner, sv._admit(context, sv.ROUTING_RECURSION_LIMIT)
        except (ThreadAccessError, AttributeError):
            return None, "recovery_unavailable"
        except sv.SupervisorError as error:
            return None, error.code

    def _owned(self, owner: str, recovery_id: object, *, reap: bool = True) -> _Entry | None:
        entry = self._entries.get(recovery_id) if isinstance(recovery_id, str) else None
        if entry is None or not hmac.compare_digest(entry.record.owner, owner):
            return None
        if reap:  # cancel() records its intent first, then settles (see _request_cancel)
            self._reap(entry)
        if entry.record.lifecycle in ("paused", "completed", "failed") and self._clock() >= entry.record.expires_at:  # fmt: skip
            self._set(entry, "expired", code="expired")
        return entry

    def _set(self, entry: _Entry, lifecycle: Lifecycle, **fields) -> None:
        rec = entry.record
        update = {"lifecycle": lifecycle, "generation": rec.generation + 1, **fields}
        if lifecycle != "paused":
            update |= {"next_stage": None, "budget_left_seconds": None}
        if lifecycle in ("cancelled", "expired", "superseded", "failed", "completed"):
            if entry.custody is not None:
                entry.custody.clear()
            entry.custody = None
        if lifecycle in ("cancelled", "expired", "superseded"):
            entry.result = None
        entry.record = rec.model_copy(update=update)
        RecoveryRecord.model_validate(entry.record.model_dump())  # never store an invalid one

    def _reap(self, entry: _Entry) -> bool:
        """D50-01: settle a ``running`` entry whose task can never run again: the task is
        done without having published (it ended abruptly), or its event loop is closed. Only
        then is custody cleared, because nothing can still use it. A pending task on an open
        loop is never touched, however long it runs (its own deadline bounds it). The entry
        becomes ``cancelled`` (if cancellation was requested) or ``failed``/``interrupted``,
        with no result, and is dropped at the next prune; it never becomes completed."""
        task, loop = entry.task, entry.loop
        if entry.record.lifecycle != "running" or task is None:
            return False  # not running, or between insertion and task start (same step)
        if not (task.done() or loop is None or loop.is_closed()):
            return False
        cancelled = entry.cancel_requested
        entry.task = entry.loop = None
        code = "cancelled" if cancelled else "interrupted"
        self._set(entry, "cancelled" if cancelled else "failed", code=code, expires_at=self._clock())  # fmt: skip
        rec = entry.record
        self._event(operation="reap", outcome=rec.lifecycle, code=code, generation=rec.generation, attempt=rec.attempt_id)  # fmt: skip
        return True

    def _request_cancel(self, entry: _Entry) -> bool:
        """D50-02: record the cancellation of a running entry and deliver it to the task's
        own loop. A closed loop cannot take the callback (``RuntimeError``): the intent stays
        recorded and the entry is settled by ``_reap``, since that task can never run again.
        Returns True when the entry settled here; False means requested, not yet settled.
        Settled means the local task cannot continue; a tool request already sent to the MCP
        host is not recalled (tools are read-only)."""
        first, entry.cancel_requested = not entry.cancel_requested, True
        if entry.task is None:
            return False  # _run sees cancel_requested when it starts
        if first:  # a repeated request is acknowledged, not resent
            try:
                entry.loop.call_soon_threadsafe(entry.task.cancel)
            except (RuntimeError, AttributeError):  # loop closed (or gone): reaped below
                pass
        return self._reap(entry)

    def _prune(self, everything: bool = False) -> int:
        """Drop lapsed (or, at shutdown, all) entries that are not running, clearing their
        custody. A running entry is never dropped while its task may still use its custody;
        one whose task can never run again is settled first (``_reap``)."""
        now, dropped = self._clock(), 0
        for key, e in list(self._entries.items()):
            self._reap(e)
            if e.record.lifecycle != "running" and (everything or now >= e.record.expires_at):
                if e.custody is not None:
                    e.custody.clear()
                e.custody, e.result = None, None
                del self._entries[key]
                dropped += 1
        return dropped

    def _insert(self, entry: _Entry) -> bool:
        self._prune()  # bounded growth: drop what has lapsed
        if len(self._entries) >= self._max:
            return False
        self._entries[entry.record.recovery_id] = entry
        return True

    def _new(self, owner, admitted, context, restarts=0) -> _Entry:
        record = RecoveryRecord(
            recovery_id=secrets.token_urlsafe(24),
            owner=owner,
            identity=admitted["identity"],
            rules=admitted["rules"],
            horizon_start=context.horizon_start,
            lifecycle="running",
            expires_at=self._clock() + self._ttl,
            restarts=restarts,
        )
        return _Entry(record, sv._Custody())

    async def _run(self, entry: _Entry, context, operation: Operation, restarted_from=None):
        custody = entry.custody
        rid = entry.record.recovery_id
        with self._lock:
            if entry.cancel_requested:  # requested (from another thread) before the task began
                self._set(entry, "cancelled", code="cancelled")
                return RecoveryResult(operation=operation, outcome="cancelled", code="cancelled", recovery_id=rid, restarted_from=restarted_from, generation=entry.record.generation)  # fmt: skip
            entry.task, entry.loop = asyncio.current_task(), asyncio.get_running_loop()
        custody.run_kind = operation if operation in ("resume", "restart") else "start"
        try:
            result = await sv._execute(sv._execution_graph(custody), context, custody)
        except asyncio.CancelledError:  # user cancellation or the caller's own: never completed
            with self._lock:
                entry.task = entry.loop = None
                if entry.record.lifecycle == "running":  # not already settled by _reap
                    self._set(entry, "cancelled", code="cancelled")  # settled: custody cleared
                generation = entry.record.generation
            self._event(operation=operation, outcome="cancelled", code="cancelled", generation=generation, attempt=custody.attempt_id)  # fmt: skip
            raise
        except Exception:
            result = None
        except BaseException:  # D50-01: abrupt teardown (GeneratorExit, ...): never left running
            with self._lock:
                entry.task = entry.loop = None
                if entry.record.lifecycle == "running":
                    cancelled = entry.cancel_requested
                    self._set(entry, "cancelled" if cancelled else "failed", code="cancelled" if cancelled else "interrupted", expires_at=self._clock())  # fmt: skip
            raise
        base = {"operation": operation, "recovery_id": rid, "restarted_from": restarted_from}
        with self._lock:  # task end and lifecycle publication are one step
            entry.task = entry.loop = None
            if entry.record.lifecycle != "running":  # settled elsewhere: a late result is dropped
                rec = entry.record
                outcome = {"cancelled": "cancelled", "expired": "expired"}.get(rec.lifecycle, "failed")  # fmt: skip
                return RecoveryResult(**base, outcome=outcome, code=rec.code or outcome, generation=rec.generation, attempt_id=rec.attempt_id)  # fmt: skip
            if entry.cancel_requested:  # the task swallowed the cancel: its result is dropped
                self._set(entry, "cancelled", code="cancelled")
                return RecoveryResult(**base, outcome="cancelled", code="cancelled", generation=entry.record.generation)  # fmt: skip
            if result is not None and result.error == "paused" and custody.paused_at is not None:
                self._set(
                    entry,
                    "paused",
                    attempt_id=custody.attempt_id,
                    next_stage=custody.paused_at,
                    budget_left_seconds=min(custody.budget_left, sv.DEFAULT_TIMEOUT),
                    expires_at=self._clock() + self._ttl,
                )
                rec = entry.record
                return RecoveryResult(**base, outcome="paused", generation=rec.generation, attempt_id=rec.attempt_id, next_stage=rec.next_stage)  # fmt: skip
            attempt = custody.attempt_id
            if result is None or result.error == "stale_recovery":
                code = "stale_recovery" if result is not None else "internal_error"
                self._set(entry, "failed", attempt_id=attempt, code=code, expires_at=self._clock() + self._ttl)  # fmt: skip
                outcome = "recovery_refused" if code == "stale_recovery" else "failed"
                return RecoveryResult(**base, outcome=outcome, code=code, generation=entry.record.generation, attempt_id=attempt)  # fmt: skip
            failed = result.status == "error" or result.route.action == "failed"
            code = (result.error or result.route.reason or "failed") if failed else None
            lifecycle = "failed" if failed else "completed"
            self._set(entry, lifecycle, attempt_id=result.attempt_id or attempt, code=code, expires_at=self._clock() + self._ttl)  # fmt: skip
            entry.result = result
            rec = entry.record
            return RecoveryResult(**base, outcome=lifecycle, code=code, generation=rec.generation, attempt_id=rec.attempt_id, result=result)  # fmt: skip

    def _stale(self, rec: RecoveryRecord, admitted, context) -> Refusal | None:
        identity = admitted["identity"]
        if identity.model_copy(update={"source_sha256": rec.identity.source_sha256}) != rec.identity:  # fmt: skip
            return "identity_mismatch"
        if identity.source_sha256 != rec.identity.source_sha256:
            return "source_changed"
        if tuple(admitted["rules"]) != rec.rules:
            return "rules_changed"
        if context.horizon_start != rec.horizon_start:
            return "policy_changed"
        return None

    # -- operations -------------------------------------------------------------------------

    @_reported
    async def start(self, context, *, pause_before: Stage | None = None) -> RecoveryResult:
        """Begin a new attempt; with ``pause_before`` it stops at that stage boundary."""
        if pause_before is not None and pause_before not in STAGES:
            raise ValueError("pause_before must be a planned stage")
        owner, admitted = self._admit(context)
        if owner is None:  # the Supervisor's own admission code (e.g. unauthenticated)
            return _refused("start", admitted)
        entry = self._new(owner, admitted, context)
        entry.custody.pause_before = pause_before
        with self._lock:
            if not self._insert(entry):
                return _refused("start", "recovery_capacity")
        return await self._run(entry, context, "start")

    @_reported
    async def resume(self, context, recovery_id: str, generation: int, *, pause_before: Stage | None = None) -> RecoveryResult:  # fmt: skip
        """Continue a paused attempt from its next stage, with its original held evidence."""
        if pause_before is not None and pause_before not in STAGES:
            raise ValueError("pause_before must be a planned stage")
        owner, admitted = self._admit(context)
        if owner is None:
            return _refused("resume", "recovery_unavailable")
        with self._lock:
            entry = self._owned(owner, recovery_id)
            if entry is None:
                return _refused("resume", "recovery_unavailable")
            rec = entry.record
            if rec.lifecycle == "running":
                return _refused("resume", "attempt_busy", recovery_id)
            if rec.lifecycle == "expired":
                return _refused("resume", "expired", recovery_id)
            if rec.lifecycle != "paused":
                return _refused("resume", "not_resumable", recovery_id)
            if type(generation) is not int or generation != rec.generation:
                return _refused("resume", "stale_generation", recovery_id)
            if entry.custody is None:
                return _refused("resume", "evidence_unavailable", recovery_id)
            problem = self._stale(rec, admitted, context)
            if problem is not None:  # the attempt stays paused for its rightful context
                return _refused("resume", problem, recovery_id)
            if not rec.budget_left_seconds:
                self._set(entry, "expired", code="expired")
                return _refused("resume", "expired", recovery_id)
            entry.custody.budget_left = rec.budget_left_seconds
            entry.custody.prepare_resume(pause_before)
            self._set(entry, "running")
        return await self._run(entry, context, "resume")

    @_reported
    async def restart(self, context, recovery_id: str) -> RecoveryResult:
        """Abandon an attempt and evaluate the RFQ again as a NEW attempt from this trusted
        context (the source may be corrected, e.g. after clarification). Never a resume."""
        owner, admitted = self._admit(context)
        if owner is None:
            return _refused("restart", "recovery_unavailable")
        with self._lock:
            old = self._owned(owner, recovery_id)
            if old is None:
                return _refused("restart", "recovery_unavailable")
            rec = old.record
            if rec.lifecycle == "running":
                return _refused("restart", "attempt_busy", recovery_id)
            if rec.lifecycle == "superseded":  # one live attempt per lineage
                return _refused("restart", "not_resumable", recovery_id)
            if rec.restarts >= MAX_RESTARTS:
                return _refused("restart", "restart_limit", recovery_id)
            new_identity = admitted["identity"]
            if (new_identity.customer_id, new_identity.rfq_id) != (rec.identity.customer_id, rec.identity.rfq_id):  # fmt: skip
                return _refused("restart", "identity_mismatch", recovery_id)
            entry = self._new(owner, admitted, context, restarts=rec.restarts + 1)
            entry.custody.previous_attempt = rec.attempt_id  # telemetry lineage only (Day 50)
            if not self._insert(entry):
                return _refused("restart", "recovery_capacity", recovery_id)
            self._set(old, "superseded", code="superseded")
        return await self._run(entry, context, "restart", restarted_from=recovery_id)

    @_reported
    def retrieve(self, principal: Principal, recovery_id: str) -> RecoveryResult:
        """The finished attempt's stored result, as of its completion; nothing re-executes and
        nothing is re-validated against today's rules or data."""
        try:
            owner = _owner(principal)
        except ThreadAccessError:
            return _refused("retrieve", "recovery_unavailable")
        with self._lock:
            entry = self._owned(owner, recovery_id)
            if entry is None:
                return _refused("retrieve", "recovery_unavailable")
            rec = entry.record
            if rec.lifecycle == "expired":
                return _refused("retrieve", "expired", recovery_id)
            if rec.lifecycle not in ("completed", "failed") or entry.result is None:
                return _refused("retrieve", "not_resumable", recovery_id)
            return RecoveryResult(operation="retrieve", outcome=rec.lifecycle, recovery_id=recovery_id, generation=rec.generation, attempt_id=rec.attempt_id, code=rec.code, result=entry.result)  # fmt: skip

    @_reported
    def cancel(self, principal: Principal, recovery_id: str) -> RecoveryResult:
        """Cancel a paused attempt (done: custody cleared, ``code=None``) or request the
        cancellation of a running one (``code="cancel_requested"``: an acknowledgement, not
        proof the task stopped; cancellation is cooperative). Once requested, the attempt can
        only end ``cancelled``: a late result or pause is discarded and custody is cleared
        when the task settles. Safe from any thread: the task is cancelled on its own loop.
        If that loop is already closed (D50-02) the task can never run again, so the attempt
        settles ``cancelled`` at once (``code=None``); nothing is raised."""
        try:
            owner = _owner(principal)
        except ThreadAccessError:
            return _refused("cancel", "recovery_unavailable")
        with self._lock:
            entry = self._owned(owner, recovery_id, reap=False)
            if entry is None:
                return _refused("cancel", "recovery_unavailable")
            rec = entry.record
            code = None
            if rec.lifecycle == "paused":
                self._set(entry, "cancelled", code="cancelled")
            elif rec.lifecycle == "running":
                code = None if self._request_cancel(entry) else "cancel_requested"
            else:
                return _refused("cancel", "not_resumable", recovery_id)
            return RecoveryResult(operation="cancel", outcome="cancelled", code=code, recovery_id=recovery_id, generation=entry.record.generation)  # fmt: skip

    def prune(self) -> int:
        """Explicit cleanup for the application's lifecycle (e.g. a periodic job): drop expired
        entries that are not running and clear their custody. Returns how many were dropped.
        Without it, cleanup happens only on access and insertion (opportunistic)."""
        with self._lock:
            return self._prune()

    def close(self) -> int:
        """Shutdown: drop every entry that is not running (custody cleared) and request the
        cancellation of running ones, whose custody is cleared when they settle. A running
        entry whose loop is closed or whose task already ended is settled and dropped too."""
        with self._lock:
            for e in list(self._entries.values()):
                if e.record.lifecycle == "running":
                    self._request_cancel(e)
            return self._prune(everything=True)

    def export(self, principal: Principal, recovery_id: str) -> str | None:
        """The attempt's persistable metadata (``RecoveryRecord`` JSON), owner only."""
        try:
            owner = _owner(principal)
        except ThreadAccessError:
            return None
        with self._lock:
            entry = self._owned(owner, recovery_id)
            return entry.record.to_json() if entry is not None else None

    def adopt(self, data: str | bytes) -> Refusal | None:
        """Load a persisted record (e.g. after a process restart). It carries no evidence, so
        a paused attempt can no longer continue, only restart; a running one was interrupted.
        Records are not authenticated (L04/L21/L26): adopting one grants nothing by itself."""
        try:
            record = RecoveryRecord.from_json(data)
        except ValueError:
            return "invalid_record"
        if record.lifecycle == "running":
            record = record.model_copy(update={"lifecycle": "failed", "code": "interrupted"})
        with self._lock:
            if record.recovery_id in self._entries:
                return "invalid_record"
            if not self._insert(_Entry(record, None)):
                return "recovery_capacity"
        return None
