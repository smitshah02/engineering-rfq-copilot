"""versioning: a new immutable version of an existing quotation, for a verified actor.

    Principal + quote id + expected current version + trusted evaluation inputs
        -> revise_quote -> quote_versions (n + 1) / quote_line_items
           + quotes pointer + idempotency receipt + audit event, in one SAVEPOINT

Source: the stored RFQ is the only trusted revision source. Nothing in the application
amends RFQ lines (there is no approved amendment protocol), so a revision re-evaluates the
same stored lines, under the same server-pinned rules, with a different evaluation context:
horizon, snapshot pins or the requested delivery date. The recomputed
``evaluation_fingerprint`` must differ from the one recorded for the current version
(``unchanged`` otherwise). If the server pins differ from the current version's rule
versions the revision is refused (``rule_version_changed``): no policy says when a quotation
may move to new rules. Agent or Supervisor output is never read.

Authority: the dataset permission ``edit_quote_draft`` (sales_representative, estimator),
re-read from the database as in draft persistence; ``create_quote_draft`` is not used. Receipts use
the approved idempotency operation ``edit_quote_draft`` (``idempotency_records`` rule_01).

State: only ``draft`` and ``revised_draft`` quotations are revised, and only while no
version is approved, nothing is released and no approval request is pending. The transition
to ``revised_draft`` must be listed by the workflow rule the current version records:
``workflow-v1.0`` lists draft -> revised_draft only; ``workflow-v1.1`` (approved 2026-11-15)
also lists revised_draft -> revised_draft. The new version records that same rule, so a
``workflow-v1.0`` quotation is never moved to ``workflow-v1.1`` (no adoption mechanism is
approved). A missing recorded rule is ``provenance_missing``; one that is not a manifest
workflow entry raises ``RuleIntegrityError``. The other pins must equal the recorded ones.
human decision: a ``rejected`` quotation is revised too, where its recorded workflow rule lists
``rejected -> revised_draft`` (workflow-v1.0 to v1.2 do; K4 ``k4_quotes_guard`` allows it).
Its rejected request and history stay as they are; K5 clears the quote's ``submitted_by``
and ``rejection_reason`` so the new version can be resubmitted. Approved, pending,
released and every other state are refused (``revision_not_allowed``): superseding an approved or pending
version needs an approval reset that K4 does not allow yet
(``version_bump_requires_approval_reset``). The new version is never approved or released.

Concurrency: the quote row is locked ``FOR NO KEY UPDATE`` before any check, so revisions of
one quotation are serialized by PostgreSQL. After the lock the expected version is compared
with the locked row (``stale_version``), a receipt for the key is replayed, and the pointer
update is also guarded by ``current_version`` and ``row_version`` in SQL. Unique
``(quote_id, version)`` and the partial unique ``is_current`` index are the backstops.

Write order: the previous version's ``is_current`` is cleared (its only change: financial
content, lines and provenance stay untouched), the new version and its lines are inserted,
then ``quotes.current_version`` moves to the new version, which already exists. The
service never commits and never logs.

integrity (K3): a revision of a ``workflow-v1.2`` quotation is ``complete``: it inserts or
reuses (verified) the RFQ's calculation record, writes its own evidence and its own
validity window (from its own database creation time), and forces the deferred
line-completeness triggers IMMEDIATE inside the savepoint. A revision of a v1.0/v1.1
quotation stays ``priced_partial`` and keeps its rule. Creation time is PostgreSQL's
``transaction_timestamp()``.

Adoption (R8): ``adopt_workflow_rule="workflow-v1.2"`` is the only way a quotation moves to
another workflow rule. Explicit, one-way (only a recorded ``workflow-v1.1`` quotation, only
to the server's draft rule), authorized and idempotent exactly as a revision (permission and
receipt operation ``edit_quote_draft``), audited as ``quote_version.adopt_workflow``. It
re-evaluates the stored RFQ under the server pins (whose manufacturing, pricing and
approval rules must equal the recorded ones), recomputes every piece of evidence, and
writes a new complete version n + 1; every earlier version is untouched. The target rule's
transitions govern the step. An unchanged evaluation may be adopted (the new version's
purpose is its evidence); an ordinary revision still refuses ``unchanged``. Workflow-v1.0
and seeded legacy quotations are never adopted (no recorded v1.1 rule, no creation event).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from rfq_copilot import quote_drafts as drafts
from rfq_copilot import quote_evidence as k3
from rfq_copilot.api.auth import Principal
from rfq_copilot.canonical import fingerprint
from rfq_copilot.models import RFQ, ApprovalRequest, AuditEvent, Quote, QuoteVersion
from rfq_copilot.quote_drafts import DraftRefused, QuoteDraft, violated_unique
from rfq_copilot.repositories import get_rfq
from rfq_copilot.repositories.idempotency import add_receipt
from rfq_copilot.repositories.queries import RuleIntegrityError
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES

OPERATION = "edit_quote_draft"  # idempotency_records rule_01 and the dataset permission
AUDIT_OPERATION = "quote_version.create"
ADOPT_OPERATION = "quote_version.adopt_workflow"  # integrity R8; audit_events.operation is text
ADOPTABLE_FROM = frozenset({"workflow-v1.1"})  # never v1.0 or an unrecorded rule
# Success events that record a version's creation (draft, revision, adoption).
CREATION_OPERATIONS = (drafts.AUDIT_OPERATION, AUDIT_OPERATION, ADOPT_OPERATION)
REQUEST_VERSION = "revise-quote-v1"
REVISABLE = frozenset({"draft", "revised_draft", "rejected"})  # rejected: human decision
REVISED = "revised_draft"
# Revision 20261108_01: a lost race on the version number, its derived primary key
# (``{quote_id}-V{n}``) or the current flag.
VERSION_CONSTRAINTS = frozenset(
    {"pk_quote_versions", "uq_quote_versions_quote_id_version", "uq_quote_versions_current"}
)


@dataclass(frozen=True, kw_only=True)
class QuoteRevision(QuoteDraft):
    """A persisted revision: QuoteDraft's customer-safe fields plus the version it replaced."""

    previous_version: int


def revision_digest(
    principal: Principal, quote_id: str, expected: int, inputs: dict, adopt: str | None = None
) -> str:
    """What one key promises: same customer, quotation, expected version, inputs and (only
    when requested) the adopted workflow rule; an ordinary revision's digest is unchanged."""
    return fingerprint(
        {
            "request_version": REQUEST_VERSION,
            "customer_id": principal.customer_id,
            "quote_id": quote_id,
            "expected_version": expected,
            **inputs,
            **({"adopt_workflow_rule": adopt} if adopt is not None else {}),
        }
    )


async def _locked_quote(
    session, principal: Principal, quote_id: str, *, share: bool = False
) -> Quote | None:
    """The customer's quote, re-read (not the identity map) and row-locked: FOR NO KEY
    UPDATE to revise, or FOR SHARE (``share``) so no revision can commit while a reader
    holds it."""
    return await session.scalar(
        select(Quote)
        .join(RFQ, RFQ.rfq_id == Quote.rfq_id)
        .where(Quote.quote_id == quote_id, RFQ.customer_id == principal.customer_id)
        .options(raiseload("*"))
        .with_for_update(read=share, key_share=not share, of=Quote)
        .execution_options(populate_existing=True)
    )


def recorded_workflow_version(version: QuoteVersion) -> str:
    """The workflow rule a version records; it must be a manifest workflow entry."""
    name = version.workflow_rule_version
    if name is None:
        raise DraftRefused("provenance_missing")
    if TRUSTED_RULES.get(name, ("",))[0] != "workflow":
        raise RuleIntegrityError(f"Rule {name} is not an approved workflow rule.")
    return name


async def _recorded_fingerprint(session, quote_id: str, version: int) -> str:
    return (await recorded_event(session, quote_id, version))["evaluation_fingerprint"]


async def recorded_event(session, quote_id: str, version: int) -> dict:
    """The one success audit payload this service family recorded for the version."""
    rows = (
        await session.scalars(
            select(AuditEvent.event_data).where(
                AuditEvent.quote_id == quote_id,
                AuditEvent.quote_version == version,
                AuditEvent.operation.in_(CREATION_OPERATIONS),
                AuditEvent.outcome == "success",
            )
        )
    ).all()
    if len(rows) != 1 or not isinstance(rows[0].get("evaluation_fingerprint"), str):
        raise DraftRefused("provenance_missing")  # e.g. a seeded historical quotation
    return rows[0]


async def revise_quote(
    session: AsyncSession,
    principal: Principal,
    quote_id: str,
    *,
    expected_version: int,
    idempotency_key: str,
    correlation_id: str,
    horizon_start: date | None = None,
    requested_delivery_date: date | None = None,
    contradictions: Sequence[str] = (),
    inventory_snapshot: str | None = None,
    capacity_snapshot: str | None = None,
    expected_evaluation_fingerprint: str | None = None,
    adopt_workflow_rule: str | None = None,
) -> QuoteRevision:
    """Persist (flush, not commit) version n + 1, or replay this key's earlier revision.
    ``adopt_workflow_rule`` requests the explicit one-way workflow adoption (module doc)."""
    if isinstance(contradictions, str) or type(expected_version) is not int or expected_version < 1:
        raise DraftRefused("invalid_request")
    if adopt_workflow_rule is not None and not isinstance(adopt_workflow_rule, str):
        raise DraftRefused("invalid_request")
    inputs = {
        "horizon_start": horizon_start,
        "requested_delivery_date": requested_delivery_date,
        "contradictions": tuple(contradictions),
        "inventory_snapshot": inventory_snapshot,
        "capacity_snapshot": capacity_snapshot,
        "expected_evaluation_fingerprint": expected_evaluation_fingerprint,
    }
    drafts._valid_request(principal, quote_id, idempotency_key, correlation_id, inputs)
    digest = revision_digest(principal, quote_id, expected_version, inputs, adopt_workflow_rule)
    customer, key = principal.customer_id, idempotency_key

    await drafts._authorize(session, principal, OPERATION)
    quote = await _locked_quote(session, principal, quote_id)  # held until the caller ends
    if quote is None:
        raise DraftRefused("not_found")  # absent or another customer's: identical
    details = await get_rfq(
        session, quote.rfq_id, customer_id=customer, assigned_user=principal.assigned_user
    )
    if details is None:
        raise DraftRefused("not_found")  # unassigned RFQ
    replay = await drafts._replay(session, principal, key, digest, OPERATION, QuoteRevision)
    if replay is not None:
        return replay
    if quote.current_version != expected_version:
        raise DraftRefused("stale_version")
    pending = await session.scalar(
        select(ApprovalRequest.approval_request_id)
        .where(ApprovalRequest.quote_id == quote_id, ApprovalRequest.status == "pending")
        .limit(1)
    )
    current = await session.scalar(
        select(QuoteVersion)
        .where(QuoteVersion.quote_id == quote_id, QuoteVersion.version == expected_version)
        .options(raiseload("*"))
        .execution_options(populate_existing=True)
    )
    if (
        quote.status not in REVISABLE
        or quote.approved_version is not None
        or quote.released_at is not None
        or pending is not None
        or current is None
        or not current.is_current
        or current.approval_valid
    ):
        raise DraftRefused("revision_not_allowed")
    if quote.status == "rejected":  # K5: only a genuine rejection of this exact version
        rejected = await session.scalar(
            select(ApprovalRequest.approval_request_id)
            .where(
                ApprovalRequest.quote_id == quote_id,
                ApprovalRequest.quote_version == expected_version,
                ApprovalRequest.status == "rejected",
            )
            .limit(1)
        )
        if rejected is None:
            raise DraftRefused("revision_not_allowed")
    if not details.runtime_lines:
        raise DraftRefused("not_draftable")

    # The quotation's own recorded workflow rule decides; it is never upgraded silently. Only
    # an explicit adoption of the server's draft rule, from a recorded v1.1, moves it.
    workflow_version = recorded_workflow_version(current)
    target = workflow_version
    if adopt_workflow_rule is not None:
        if (
            adopt_workflow_rule != drafts.WORKFLOW_RULE_VERSION
            or workflow_version not in ADOPTABLE_FROM
        ):
            raise DraftRefused("revision_not_allowed")
        target = adopt_workflow_rule
    now = await k3.database_now(session)  # one trusted creation time (K3, R3)
    if inputs["horizon_start"] is None:  # 54C: server-dated context (as drafts)
        inputs = {**inputs, "horizon_start": k3.server_horizon(now)}
    rules, workflow, evaluation, quotation, pricing = await drafts._evaluate(
        session, principal, quote.rfq_id, inputs, target
    )
    if adopt_workflow_rule is not None and k3.validity_dates(workflow) is None:
        raise DraftRefused("revision_not_allowed")  # adoption exists to gain evidence
    # The governing rule's transitions: the recorded one, or the adopted target (R8).
    if REVISED not in workflow.document.get("transitions", {}).get(quote.status, ()):
        raise DraftRefused("revision_not_allowed")  # e.g. revised_draft under workflow-v1.0
    versions = drafts.rule_versions(rules, workflow)
    recorded = (
        current.manufacturing_rule_version,
        current.pricing_rule_version,
        current.approval_rule_version,
        current.workflow_rule_version,
    )
    if versions[:3] != recorded[:3] or (target == workflow_version and versions != recorded):
        raise DraftRefused("rule_version_changed")
    # Also proves a draft persistence+ quotation: a seeded legacy one has no creation event.
    previous = await _recorded_fingerprint(session, quote_id, expected_version)
    if adopt_workflow_rule is None and previous == evaluation.evaluation_fingerprint:
        raise DraftRefused("unchanged")  # adoption alone may keep the same evaluation

    number = expected_version + 1
    # K5: a rejected quotation's submission and rejection are cleared by its revision (the
    # reason stays in approval_history), so the new version can be submitted afresh.
    cleared = {}
    if quote.status == "rejected":
        cleared = {"submitted_by": None, "rejection_reason": None}
    revision = QuoteRevision(
        quote_id=quote_id,
        rfq_id=quote.rfq_id,
        customer_id=customer,
        version=number,
        status=REVISED,
        currency=pricing.currency,
        total_usd=quotation.quote_total_usd,
        calculation_id=pricing.calculation_id,
        evaluation_fingerprint=evaluation.evaluation_fingerprint,
        outcome=evaluation.outcome,
        manager_review_required=evaluation.manager_review_required,
        manager_review_reasons=evaluation.manager_review_reasons,
        rule_versions=versions,
        expiry_date=drafts.expected_expiry(workflow, now),
        previous_version=expected_version,
    )
    try:
        async with session.begin_nested():  # any failure undoes every row below
            # The K3 line-completeness triggers are checked here, never at the caller's COMMIT.
            await session.execute(text(f"SET CONSTRAINTS {k3.LINE_CONSTRAINTS} DEFERRED"))
            current.is_current = False  # the previous version's only change
            await session.flush()
            evidence = await drafts.complete_evidence(
                session, principal.user_id, quote.rfq_id, customer, details, now, rules,
                workflow, evaluation, quotation, pricing, inputs,
            )  # fmt: skip
            version, lines = drafts.version_rows(
                quote_id, number, principal.user_id, now,
                rules, workflow, evaluation, quotation, pricing, evidence,
            )  # fmt: skip
            session.add(version)
            await session.flush()
            session.add_all(lines)
            await session.flush()
            await session.execute(text(f"SET CONSTRAINTS {k3.LINE_CONSTRAINTS} IMMEDIATE"))
            moved = await session.execute(
                update(Quote)
                .where(
                    Quote.quote_id == quote_id,
                    Quote.current_version == expected_version,
                    Quote.row_version == quote.row_version,
                )
                .values(
                    current_version=number,
                    status=REVISED,
                    row_version=Quote.row_version + 1,
                    **cleared,
                )
                .execution_options(synchronize_session=False)
            )
            if moved.rowcount != 1:
                raise DraftRefused("stale_version")  # unreachable under the row lock
            data = {
                **drafts.event_data(revision, principal, inputs),
                "previous_version": expected_version,
            }
            operation = AUDIT_OPERATION
            if adopt_workflow_rule is not None:
                data.update(adopted_from=workflow_version, adopted_to=target)
                operation = ADOPT_OPERATION
            session.add(
                AuditEvent(
                    actor_id=principal.user_id,
                    customer_id=customer,
                    rfq_id=quote.rfq_id,
                    quote_id=quote_id,
                    quote_version=number,
                    operation=operation,
                    outcome="success",
                    correlation_id=correlation_id,
                    event_data=data,
                )
            )
            add_receipt(
                session,
                actor_id=principal.user_id,
                customer_id=customer,
                key=key,
                request_sha256=digest,
                result=revision.to_json(),
                correlation_id=correlation_id,
                operation=OPERATION,
            )
            await session.flush()
    except IntegrityError as error:
        # Savepoint rolled back. Only named identity constraints are a lost race.
        constraint = violated_unique(error)
        if constraint == drafts.RECEIPT_KEY:
            replay = await drafts._replay(session, principal, key, digest, OPERATION, QuoteRevision)  # fmt: skip
            if replay is not None:
                return replay
        if constraint in VERSION_CONSTRAINTS:
            raise DraftRefused("stale_version") from None
        raise
    return revision
