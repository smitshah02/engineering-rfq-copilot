"""integrity: human submission, approval and rejection of one persisted quotation version.

    Principal + quote id + expected current version (+ attestation / reason)
        -> submit_for_review / approve_quote / reject_quote -> QuoteDecision
           + approval_requests, approval_history, quotes, quote_versions.approval_valid,
             audit event and idempotency receipt, in one SAVEPOINT of the caller's transaction

Human only. Every decision needs an active ``business.users`` row, re-read in this
transaction, whose role the database grants the operation (``request_approval``,
``approve_quote``, ``reject_quote``) and, for decisions, whose role is the approved
contract's approver role (``sales_manager``: approval_requests rule_02). No agent, Supervisor
or MCP tool holds a users row or these grants (``services.evaluation.AGENT_MUST_NOT``), none
calls this module, and nothing here reads agent output, a caller-supplied assessment or
the Principal's permission set. The assessment is recomputed here (``quote_approval_policy``)
inside the decision's own transaction and row lock (FOR NO KEY UPDATE), and must find the
expected current version, exactly one pending request for it, consistent evidence and no
binding blocker other than the attestation this decision supplies.

Binding work runs only on the restricted decision role (K5): ``approve_quote``,
``reject_quote`` and ``release_quote`` call ``database.require_decision_runtime`` before
anything else (the triggers do not bind the table owner, and K5 refuses decisions from any
role outside ``rfq_quote_decision``). Submission stays with the ordinary runtime role. The K4 triggers then bind every row:
the quote can move only together with the request (or, for a release, the history row)
resolved in the same transaction for the same version; the version's ``approval_valid``
flips once and only with an approved request; a request resolves once.

Policy (integrity decisions, fail closed where the roadmap left a gap):
- a pending request (``submit_for_review``) is mandatory before any decision;
- the approver must differ from the quote's creator (quotes rule_03), from the version's
  creator and from the requester (``separation_of_duties``);
- a required engineering review has no evidence record anywhere, so a version that needs
  one is never approved (``review_unverified``); a manager review is the approver's own;
- 54C: the version's context must be server-dated (``EVALUATION_CONTEXT_NOT_AUTHENTICATED``
  otherwise), and the approver attests, bound to the version's K2-protected RFQ source
  digest, the requested delivery date and the absence of contradictions
  (``ContextAttestation``; ``attestation_mismatch`` otherwise). The attestation is
  recorded in ``approval_history.reason`` and the audit event;
- rejection moves the quote to ``rejected`` (workflow-v1.2: ``rejected -> revised_draft``),
  the request to ``rejected``; a new request needs a new version.

Idempotency and races: a key is required; the receipt is unique per (actor, customer,
operation, key) and the request digest covers every input. Same key and same request:
the stored decision is returned (``replayed``); same key, another request:
``idempotency_key_conflict``. A lost race on the pending-request index or the quote row is
``stale_version``/``not_pending``, never a second decision. Results are customer-safe:
no cost, margin or RFQ text. The service never commits and never logs.

human decision lifecycle. Allowed: draft/revised_draft -> pending_approval (submit),
pending_approval -> approved | rejected (decide), rejected -> revised_draft (revision,
``quote_versions``), approved -> released (``release_quote``). Every other jump is refused
by the services and, behind them, by ``k4_quotes_guard``.

- Release (``release_quote``; dataset permission ``release_quote``, held only by
  ``sales_manager``; idempotency operation ``release_quote``; workflow-v1.2
  ``approved -> released``; K4 release branch): restricted runtime role first, grant
  re-read, quote row locked, the assessment recomputed for the approved current version,
  and ``release_blockers`` must be empty: no binding blocker other than the attestation the
  approval already recorded (so expired -> ``quote_expired``, changed evaluation, integrity
  or evidence mismatch -> ``not_releasable``), a reviewable policy outcome, no engineering
  review, and exactly one approved request for the version, resolved by the quote's
  recorded approver, with its attested ``approved`` history row. Release changes only the
  quote status and writes the ``released`` history row, audit event and receipt. Nothing is
  sent, delivered or dispatched. The releaser must differ from the approver
  (``separation_of_duties``; human decision business decision, also enforced by ``k5_quotes_guard``).
- ``assess_release`` is the internal, read-only operator view of the same checks
  (``view_assigned_rfqs``): codes only. The customer sees a quotation only through the
  existing released-quote API (``api/routers/quotes.py``), after a release.
- Resubmission after rejection (K5): ``revise_quote`` of a rejected quotation clears
  ``submitted_by`` and ``rejection_reason`` (the text stays in ``approval_history``), so the
  new version is submitted and freshly approved. ``resubmission_blocked`` remains only for
  a quotation revised under K4, which kept its rejection reason and can never be approved.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from rfq_copilot import quote_approval_policy as policy
from rfq_copilot import quote_drafts as drafts
from rfq_copilot import quote_evidence as k3
from rfq_copilot.api.auth import Principal
from rfq_copilot.canonical import fingerprint
from rfq_copilot.database import require_decision_runtime
from rfq_copilot.models import (
    ApprovalHistory,
    ApprovalRequest,
    AuditEvent,
    Quote,
    QuoteVersion,
    User,
)
from rfq_copilot.quote_drafts import DraftRefused, violated_unique
from rfq_copilot.repositories.idempotency import add_receipt
from rfq_copilot.repositories.queries import _identifier

# Permissions (dataset roles_permissions) = idempotency_records.operation values.
SUBMIT, APPROVE, REJECT = "request_approval", "approve_quote", "reject_quote"
RELEASE = "release_quote"
APPROVER_ROLE = policy.APPROVER_ROLE
AUDIT = {
    SUBMIT: "quote_approval.submit",
    APPROVE: "quote_approval.approve",
    REJECT: "quote_approval.reject",
    RELEASE: "quote_approval.release",
}
REQUEST_VERSION = "quote-decision-v1"
PENDING_STATUSES = frozenset({"pending_approval"})
APPROVED_STATUSES = frozenset({"approved"})
ATTESTED = "approved; attested "  # prefix of an approval's history reason
ATTESTATION_BLOCKER = "CONTEXT_ATTESTATION_REQUIRED"  # cleared by the recorded approval
# K5 commit-time bindings (deferred constraint triggers): DEFERRED while a step writes its
# rows, IMMEDIATE before it finishes, so a violation stays inside the step's savepoint.
DECISION_CONSTRAINTS = "business.k5_approval_history_bound, business.k5_approval_request_bound"
DEFER_BINDINGS = text(f"SET CONSTRAINTS {DECISION_CONSTRAINTS} DEFERRED")
CHECK_BINDINGS = text(f"SET CONSTRAINTS {DECISION_CONSTRAINTS} IMMEDIATE")
# Revision 20261108_01 / 20261115_01: a lost race on the one-pending index or the quote.
PENDING_KEY = "uq_approval_requests_pending"
Action = Literal["submitted", "approved", "rejected", "released"]


@dataclass(frozen=True)
class ContextAttestation:
    """What the approving human vouches for, bound to the version's RFQ source digest."""

    rfq_source_sha256: str
    requested_delivery_date: date | None
    no_contradictions: bool

    def to_json(self) -> dict:
        delivery = self.requested_delivery_date
        return {
            "rfq_source_sha256": self.rfq_source_sha256,
            "requested_delivery_date": None if delivery is None else delivery.isoformat(),
            "no_contradictions": self.no_contradictions,
        }


@dataclass(frozen=True, kw_only=True)
class QuoteDecision:
    """One recorded human lifecycle step. Customer-safe: no cost, margin or RFQ text."""

    quote_id: str
    rfq_id: str
    customer_id: str
    version: int
    action: Action
    status: str  # the quote's status after this step
    approval_request_id: str
    quote_row_version: int
    actor_id: str
    assessment_fingerprint: str
    replayed: bool = False

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "replayed"}

    @classmethod
    def from_json(cls, data: dict) -> "QuoteDecision":
        return cls(**data, replayed=True)


def _valid(principal, quote_id, expected_version, key, correlation_id) -> None:
    try:
        if not isinstance(principal, Principal) or principal.user_id is None:
            raise ValueError
        _identifier(principal.user_id)
        _identifier(quote_id)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError
        if not isinstance(key, str) or not 0 < len(key) <= 256 or not key.isascii():
            raise ValueError
        if not key.isprintable() or key != key.strip():
            raise ValueError
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise ValueError
    except ValueError:
        raise DraftRefused("invalid_request") from None


def _valid_attestation(attestation) -> None:
    if (
        not isinstance(attestation, ContextAttestation)
        or not isinstance(attestation.rfq_source_sha256, str)
        or not drafts._SHA256.fullmatch(attestation.rfq_source_sha256)
        or attestation.no_contradictions is not True
        or (
            attestation.requested_delivery_date is not None
            and type(attestation.requested_delivery_date) is not date
        )
    ):
        raise DraftRefused("invalid_request")


def _valid_reason(reason) -> None:
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise DraftRefused("invalid_request")
    if not reason.isprintable():
        raise DraftRefused("invalid_request")


def decision_digest(principal: Principal, quote_id, expected_version, operation, extra) -> str:
    return fingerprint(
        {
            "request_version": REQUEST_VERSION,
            "operation": operation,
            "actor_id": principal.user_id,
            "customer_id": principal.customer_id,
            "quote_id": quote_id,
            "expected_version": expected_version,
            **extra,
        }
    )


async def _approver(session: AsyncSession, principal: Principal) -> None:
    """A decision needs the approver role itself, not only the grant (rule_02)."""
    role = await session.scalar(
        select(User.role).where(User.user_id == principal.user_id, User.active.is_(True))
    )
    if role != APPROVER_ROLE:
        raise DraftRefused("permission_denied")


def _separated(principal: Principal, quote: Quote, version: QuoteVersion, request) -> None:
    others = {quote.created_by, version.created_by, request.requested_by if request else None}
    if principal.user_id in others:
        raise DraftRefused("separation_of_duties")


async def _replay(session, principal, key, digest, operation):
    return await drafts._replay(session, principal, key, digest, operation, QuoteDecision)


async def _replay_after_serialization(session, principal, key, digest, operation, refused):
    """Only these assessment refusals occur after the scoped quote lock is acquired.

    A twin may have committed while we waited. READ COMMITTED now sees its receipt;
    different content still raises idempotency_key_conflict. Authorization, scope,
    separation and evidence refusals are never replaced by a successful replay.
    """
    if refused.reason not in {"not_assessable", "not_pending", "stale_version"}:
        return None
    await drafts._authorize(session, principal, operation)
    if operation in {APPROVE, REJECT}:
        await require_decision_runtime(session)
        await _approver(session, principal)
    return await _replay(session, principal, key, digest, operation)


def _history(quote_id, version, action, actor, now, reason, request_id) -> ApprovalHistory:
    return ApprovalHistory(
        approval_id=f"APH-{secrets.token_hex(16).upper()}",
        quote_id=quote_id,
        quote_version=version,
        action=action,
        actor_id=actor,
        action_at=now,
        reason=reason,
        approval_request_id=request_id,
    )


def _audit(principal, quote, version, operation, correlation_id, data) -> AuditEvent:
    return AuditEvent(
        actor_id=principal.user_id,
        customer_id=principal.customer_id,
        rfq_id=quote.rfq_id,
        quote_id=quote.quote_id,
        quote_version=version,
        operation=AUDIT[operation],
        outcome="success",
        correlation_id=correlation_id,
        event_data=data,
    )


async def _move_quote(session, quote: Quote, expected_version: int, **values) -> None:
    moved = await session.execute(
        update(Quote)
        .where(
            Quote.quote_id == quote.quote_id,
            Quote.current_version == expected_version,
            Quote.row_version == quote.row_version,
        )
        .values(row_version=Quote.row_version + 1, **values)
        .execution_options(synchronize_session=False)
    )
    if moved.rowcount != 1:
        raise DraftRefused("stale_version")  # unreachable under the row lock


async def _resolve_request(session, request: ApprovalRequest, status: str, actor, now) -> None:
    resolved = await session.execute(
        update(ApprovalRequest)
        .where(
            ApprovalRequest.approval_request_id == request.approval_request_id,
            ApprovalRequest.quote_id == request.quote_id,
            ApprovalRequest.quote_version == request.quote_version,
            ApprovalRequest.status == "pending",
        )
        .values(status=status, resolved_by=actor, resolved_at=now)
        .execution_options(synchronize_session=False)
    )
    if resolved.rowcount != 1:
        raise DraftRefused("not_pending")  # unreachable under the quote lock


def _decision(assessment, quote, version, action, status, request_id, principal, now_row):
    return QuoteDecision(
        quote_id=quote.quote_id,
        rfq_id=quote.rfq_id,
        customer_id=principal.customer_id,
        version=version,
        action=action,
        status=status,
        approval_request_id=request_id,
        quote_row_version=now_row,
        actor_id=principal.user_id,
        assessment_fingerprint=assessment.assessment_fingerprint,
    )


async def _finish(
    session, principal, key, digest, operation, decision, correlation_id, quote, data
):
    await session.flush()
    await session.execute(CHECK_BINDINGS)  # K5: history/request bound to the quote, now
    session.add(_audit(principal, quote, decision.version, operation, correlation_id, data))
    add_receipt(
        session,
        actor_id=principal.user_id,
        customer_id=principal.customer_id,
        key=key,
        request_sha256=digest,
        result=decision.to_json(),
        correlation_id=correlation_id,
        operation=operation,
    )
    await session.flush()


def _check_submission(assessment, quote) -> None:
    if quote.rejection_reason is not None:
        raise DraftRefused("resubmission_blocked")
    if assessment.human_review != "eligible" or assessment.integrity_findings:
        raise DraftRefused("not_submittable")


def _check_approval(assessment) -> None:
    if assessment.classification not in policy.REVIEWABLE or assessment.integrity_findings:
        raise DraftRefused("not_approvable")
    if any(r.kind == "engineering_review" for r in assessment.policy.reviews):
        raise DraftRefused("review_unverified")
    if set(assessment.binding_approval_blockers) != {ATTESTATION_BLOCKER}:
        raise DraftRefused("not_approvable")


async def submit_for_review(
    session: AsyncSession,
    principal: Principal,
    quote_id: str,
    *,
    expected_version: int,
    idempotency_key: str,
    correlation_id: str,
) -> QuoteDecision:
    """Open the one pending approval request of the current version (flush, not commit)."""
    _valid(principal, quote_id, expected_version, idempotency_key, correlation_id)
    digest = decision_digest(principal, quote_id, expected_version, SUBMIT, {})
    key = idempotency_key
    await drafts._authorize(session, principal, SUBMIT)
    replay = await _replay(session, principal, key, digest, SUBMIT)
    if replay is not None:
        return replay
    try:
        assessment, quote, version, _ = await policy._assessed(
            session, principal, quote_id, expected_version, permission=SUBMIT, share=False
        )
    except DraftRefused as refused:
        replay = await _replay_after_serialization(session, principal, key, digest, SUBMIT, refused)
        if replay is not None:
            return replay
        raise
    _check_submission(assessment, quote)
    now = await k3.database_now(session)
    request_id = f"APR-{secrets.token_hex(16).upper()}"
    try:
        async with session.begin_nested():
            await session.execute(DEFER_BINDINGS)
            session.add(
                ApprovalRequest(
                    approval_request_id=request_id,
                    quote_id=quote_id,
                    quote_version=expected_version,
                    requested_by=principal.user_id,
                    requested_at=now,
                    required_role=APPROVER_ROLE,
                    status="pending",
                    approval_rule_version=version.approval_rule_version,
                )
            )
            # ApprovalHistory.request is viewonly: the unit of work has no dependency to
            # order these INSERTs, and the history FK to the request is immediate.
            await session.flush()
            session.add(
                _history(
                    quote_id, expected_version, "submitted", principal.user_id, now,
                    f"classification={assessment.classification}", request_id,
                )
            )  # fmt: skip
            await session.flush()
            await _move_quote(
                session, quote, expected_version, status="pending_approval",
                submitted_by=principal.user_id,
            )  # fmt: skip
            decision = _decision(
                assessment, quote, expected_version, "submitted", "pending_approval",
                request_id, principal, quote.row_version + 1,
            )  # fmt: skip
            data = {
                "approval_request_id": request_id,
                "assessment_fingerprint": assessment.assessment_fingerprint,
                "classification": assessment.classification,
                "reviews": [r.kind for r in assessment.policy.reviews],
            }
            await _finish(session, principal, key, digest, SUBMIT, decision, correlation_id, quote, data)  # fmt: skip
    except IntegrityError as error:
        constraint = violated_unique(error)
        if constraint == drafts.RECEIPT_KEY:
            replay = await _replay(session, principal, key, digest, SUBMIT)
            if replay is not None:
                return replay
        if constraint == PENDING_KEY:
            raise DraftRefused("not_submittable") from None  # a request already exists
        raise
    return decision


async def _decided(session, principal, quote_id, expected_version, operation, next_state):
    """Shared preamble of a decision: decision role, approver role, lock and recomputation."""
    await require_decision_runtime(session)  # K5: fail closed before any read of the quote
    await drafts._authorize(session, principal, operation)
    await _approver(session, principal)
    assessment, quote, version, request = await policy._assessed(
        session, principal, quote_id, expected_version, permission=operation, share=False,
        statuses=PENDING_STATUSES, next_states=(next_state,), pending_required=True,
    )  # fmt: skip
    _separated(principal, quote, version, request)
    if "QUOTE_EXPIRED" in assessment.binding_approval_blockers:
        raise DraftRefused("quote_expired")
    return assessment, quote, version, request


async def approve_quote(
    session: AsyncSession,
    principal: Principal,
    quote_id: str,
    *,
    expected_version: int,
    idempotency_key: str,
    correlation_id: str,
    attestation: ContextAttestation,
) -> QuoteDecision:
    """Approve the pending current version (flush, not commit); see the module policy."""
    _valid(principal, quote_id, expected_version, idempotency_key, correlation_id)
    _valid_attestation(attestation)
    digest = decision_digest(
        principal, quote_id, expected_version, APPROVE, {"attestation": attestation.to_json()}
    )
    key = idempotency_key
    await require_decision_runtime(session)
    await drafts._authorize(session, principal, APPROVE)
    await _approver(session, principal)
    replay = await _replay(session, principal, key, digest, APPROVE)
    if replay is not None:
        return replay
    try:
        assessment, quote, version, request = await _decided(
            session, principal, quote_id, expected_version, APPROVE, "approved"
        )
    except DraftRefused as refused:
        replay = await _replay_after_serialization(
            session, principal, key, digest, APPROVE, refused
        )
        if replay is not None:
            return replay
        raise
    _check_approval(assessment)
    if not (
        hmac.compare_digest(attestation.rfq_source_sha256, version.rfq_source_sha256)
        and attestation.requested_delivery_date == version.requested_delivery_date
        and attestation.no_contradictions is True
    ):
        raise DraftRefused("attestation_mismatch")
    now = await k3.database_now(session)
    attested = attestation.to_json()
    try:
        async with session.begin_nested():
            await session.execute(DEFER_BINDINGS)
            await _resolve_request(session, request, "approved", principal.user_id, now)
            session.add(
                _history(
                    quote_id, expected_version, "approved", principal.user_id, now,
                    ATTESTED + fingerprint(attested), request.approval_request_id,
                )
            )  # fmt: skip
            await session.flush()
            flagged = await session.execute(
                update(QuoteVersion)
                .where(
                    QuoteVersion.quote_id == quote_id,
                    QuoteVersion.version == expected_version,
                    QuoteVersion.is_current.is_(True),
                    QuoteVersion.approval_valid.is_(False),
                )
                .values(approval_valid=True)
                .execution_options(synchronize_session=False)
            )
            if flagged.rowcount != 1:
                raise DraftRefused("stale_version")
            await _move_quote(
                session, quote, expected_version, status="approved",
                approved_version=expected_version, approver_id=principal.user_id,
                approval_timestamp=now,
            )  # fmt: skip
            decision = _decision(
                assessment, quote, expected_version, "approved", "approved",
                request.approval_request_id, principal, quote.row_version + 1,
            )  # fmt: skip
            data = {
                "approval_request_id": request.approval_request_id,
                "assessment_fingerprint": assessment.assessment_fingerprint,
                "classification": assessment.classification,
                "attestation": attested,
                "manager_review_reasons": [
                    reason for r in assessment.policy.reviews if r.kind == "manager_review"
                    for reason in r.reasons
                ],
            }  # fmt: skip
            await _finish(session, principal, key, digest, APPROVE, decision, correlation_id, quote, data)  # fmt: skip
    except IntegrityError as error:
        if violated_unique(error) == drafts.RECEIPT_KEY:
            replay = await _replay(session, principal, key, digest, APPROVE)
            if replay is not None:
                return replay
        raise
    return decision


async def reject_quote(
    session: AsyncSession,
    principal: Principal,
    quote_id: str,
    *,
    expected_version: int,
    idempotency_key: str,
    correlation_id: str,
    reason: str,
) -> QuoteDecision:
    """Reject the pending current version (flush, not commit); the reason is history."""
    _valid(principal, quote_id, expected_version, idempotency_key, correlation_id)
    _valid_reason(reason)
    reason_sha256 = hashlib.sha256(reason.encode()).hexdigest()
    digest = decision_digest(
        principal, quote_id, expected_version, REJECT, {"reason_sha256": reason_sha256}
    )
    key = idempotency_key
    await require_decision_runtime(session)
    await drafts._authorize(session, principal, REJECT)
    await _approver(session, principal)
    replay = await _replay(session, principal, key, digest, REJECT)
    if replay is not None:
        return replay
    try:
        assessment, quote, version, request = await _decided(
            session, principal, quote_id, expected_version, REJECT, "rejected"
        )
    except DraftRefused as refused:
        replay = await _replay_after_serialization(session, principal, key, digest, REJECT, refused)
        if replay is not None:
            return replay
        raise
    now = await k3.database_now(session)
    try:
        async with session.begin_nested():
            await session.execute(DEFER_BINDINGS)
            await _resolve_request(session, request, "rejected", principal.user_id, now)
            session.add(
                _history(
                    quote_id, expected_version, "rejected", principal.user_id, now,
                    reason, request.approval_request_id,
                )
            )  # fmt: skip
            await session.flush()
            await _move_quote(
                session, quote, expected_version, status="rejected", rejection_reason=reason
            )
            decision = _decision(
                assessment, quote, expected_version, "rejected", "rejected",
                request.approval_request_id, principal, quote.row_version + 1,
            )  # fmt: skip
            data = {
                "approval_request_id": request.approval_request_id,
                "assessment_fingerprint": assessment.assessment_fingerprint,
                "classification": assessment.classification,
                "reason_sha256": reason_sha256,  # the text itself is approval_history.reason
            }
            await _finish(session, principal, key, digest, REJECT, decision, correlation_id, quote, data)  # fmt: skip
    except IntegrityError as error:
        if violated_unique(error) == drafts.RECEIPT_KEY:
            replay = await _replay(session, principal, key, digest, REJECT)
            if replay is not None:
                return replay
        raise
    return decision


# --- human decision: release of the approved current version ------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ReleaseEligibility:
    """Internal operator view of one approved version. Codes only: no cost, margin, RFQ
    text or evidence value. Output, never input: ``release_quote`` recomputes it."""

    quote_id: str
    rfq_id: str
    customer_id: str
    version: int
    status: str
    approval_request_id: str | None
    blockers: tuple[str, ...]
    assessment_fingerprint: str

    @property
    def eligible(self) -> bool:
        return not self.blockers


def release_blockers(assessment, approval) -> tuple[str, ...]:
    """Why this approved version may not be released now; () when it may (fail closed)."""
    found = [b for b in assessment.binding_approval_blockers if b != ATTESTATION_BLOCKER]
    checks = (
        ("POLICY_NOT_RELEASABLE", assessment.classification not in policy.REVIEWABLE),
        (
            "ENGINEERING_REVIEW_UNVERIFIED",
            any(r.kind == "engineering_review" for r in assessment.policy.reviews),
        ),
        ("HUMAN_APPROVAL_NOT_VERIFIED", approval is None),
    )
    found += [code for code, applies in checks if applies]
    return tuple(dict.fromkeys(found))


async def _verified_approval(session, quote: Quote, version: int) -> ApprovalRequest | None:
    """The one approved request of this exact version, resolved by the quote's recorded
    approver, with exactly one attested ``approved`` history row; None otherwise."""
    requests = (
        await session.scalars(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.quote_id == quote.quote_id,
                ApprovalRequest.quote_version == version,
                ApprovalRequest.status == "approved",
            )
            .options(raiseload("*"))
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(requests) != 1 or quote.approver_id is None:
        return None
    request = requests[0]
    if request.resolved_by != quote.approver_id:
        return None
    recorded = await session.scalar(
        select(func.count())
        .select_from(ApprovalHistory)
        .where(
            ApprovalHistory.approval_request_id == request.approval_request_id,
            ApprovalHistory.quote_id == quote.quote_id,
            ApprovalHistory.quote_version == version,
            ApprovalHistory.action == "approved",
            ApprovalHistory.actor_id == request.resolved_by,
            ApprovalHistory.reason.startswith(ATTESTED),
        )
    )
    return request if recorded == 1 else None


async def _release_checked(session, principal, quote_id, expected_version, *, permission, share):
    assessment, quote, _, _ = await policy._assessed(
        session, principal, quote_id, expected_version, permission=permission, share=share,
        statuses=APPROVED_STATUSES, next_states=("released",), approved=True,
    )  # fmt: skip
    approval = await _verified_approval(session, quote, expected_version)
    return assessment, quote, approval, release_blockers(assessment, approval)


async def assess_release(
    session: AsyncSession, principal: Principal, quote_id: str, *, expected_version: int
) -> ReleaseEligibility:
    """Read only (FOR SHARE until the caller's transaction ends); writes nothing."""
    assessment, quote, approval, blockers = await _release_checked(
        session, principal, quote_id, expected_version, permission=policy.PERMISSION, share=True
    )
    return ReleaseEligibility(
        quote_id=quote.quote_id,
        rfq_id=quote.rfq_id,
        customer_id=principal.customer_id,
        version=expected_version,
        status=quote.status,
        approval_request_id=None if approval is None else approval.approval_request_id,
        blockers=blockers,
        assessment_fingerprint=assessment.assessment_fingerprint,
    )


async def assess_staff_actions(session, principal: Principal, quote_id: str, version: int):
    """Limited staff projection on a lock-capable, rollback-only ordinary-role session.

    No decision credential, writes or commits. Reuse the locked service assessments and
    trusted grants; this is a momentary hint, never authority for a subsequent mutation.
    """
    results, context, reviews = {}, None, ()
    for operation in (SUBMIT, APPROVE, REJECT, RELEASE):
        try:
            await drafts._authorize(session, principal, operation)
            if operation == RELEASE:
                assessment, quote, approval, blockers = await _release_checked(
                    session, principal, quote_id, version, permission=operation, share=True
                )
                if "QUOTE_EXPIRED" in blockers:
                    raise DraftRefused("quote_expired")
                if blockers:
                    raise DraftRefused("not_releasable")
                if principal.user_id == quote.approver_id:
                    raise DraftRefused("separation_of_duties")
            else:
                deciding = operation in {APPROVE, REJECT}
                if deciding:
                    await _approver(session, principal)
                assessment, quote, stored, request = await policy._assessed(
                    session,
                    principal,
                    quote_id,
                    version,
                    permission=operation,
                    share=True,
                    statuses=PENDING_STATUSES if deciding else policy.ASSESSABLE,
                    next_states=("approved" if operation == APPROVE else "rejected",)
                    if deciding
                    else ("pending_approval",),
                    pending_required=deciding,
                )
                if deciding:
                    _separated(principal, quote, stored, request)
                    if "QUOTE_EXPIRED" in assessment.binding_approval_blockers:
                        raise DraftRefused("quote_expired")
                if operation == SUBMIT:
                    _check_submission(assessment, quote)
                if operation == APPROVE:
                    _check_approval(assessment)
                    context = {
                        "rfq_source_sha256": stored.rfq_source_sha256,
                        "requested_delivery_date": stored.requested_delivery_date,
                    }
            reviews = tuple(
                {"kind": r.kind, "reasons": r.reasons} for r in assessment.policy.reviews
            )
            results[operation] = {"allowed": True, "reason": None}
        except DraftRefused as refused:
            results[operation] = {"allowed": False, "reason": refused.reason}
    return results, context, reviews


async def release_quote(
    session: AsyncSession,
    principal: Principal,
    quote_id: str,
    *,
    expected_version: int,
    idempotency_key: str,
    correlation_id: str,
) -> QuoteDecision:
    """Release the approved current version (flush, not commit); see the module policy."""
    _valid(principal, quote_id, expected_version, idempotency_key, correlation_id)
    digest = decision_digest(principal, quote_id, expected_version, RELEASE, {})
    key = idempotency_key
    await require_decision_runtime(session)  # K5: fail closed before any read of the quote
    await drafts._authorize(session, principal, RELEASE)
    replay = await _replay(session, principal, key, digest, RELEASE)
    if replay is not None:
        return replay
    try:
        assessment, quote, approval, blockers = await _release_checked(
            session, principal, quote_id, expected_version, permission=RELEASE, share=False
        )
    except DraftRefused:
        # An identical retry that waited on the row lock finds its twin's committed receipt.
        replay = await _replay(session, principal, key, digest, RELEASE)
        if replay is not None:
            return replay
        raise
    if "QUOTE_EXPIRED" in blockers:
        raise DraftRefused("quote_expired")
    if blockers:
        raise DraftRefused("not_releasable")
    if principal.user_id == quote.approver_id:
        raise DraftRefused("separation_of_duties")  # human decision policy: releaser != approver
    now = await k3.database_now(session)
    request_id = approval.approval_request_id
    try:
        async with session.begin_nested():
            await session.execute(DEFER_BINDINGS)
            # K4: the release is bound to this 'released' history row of the approved version.
            session.add(
                _history(
                    quote_id, expected_version, "released", principal.user_id, now,
                    "released", request_id,
                )
            )  # fmt: skip
            await session.flush()
            await _move_quote(
                session, quote, expected_version, status="released",
                released_by=principal.user_id, released_at=now,
            )  # fmt: skip
            decision = _decision(
                assessment, quote, expected_version, "released", "released",
                request_id, principal, quote.row_version + 1,
            )  # fmt: skip
            data = {
                "approval_request_id": request_id,
                "assessment_fingerprint": assessment.assessment_fingerprint,
                "classification": assessment.classification,
                "approver_id": quote.approver_id,
                "approval_rule_version": approval.approval_rule_version,
            }
            await _finish(session, principal, key, digest, RELEASE, decision, correlation_id, quote, data)  # fmt: skip
    except IntegrityError as error:
        if violated_unique(error) == drafts.RECEIPT_KEY:
            replay = await _replay(session, principal, key, digest, RELEASE)
            if replay is not None:
                return replay
        raise
    return decision
