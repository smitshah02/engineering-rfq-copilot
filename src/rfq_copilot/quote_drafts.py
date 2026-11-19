"""draft persistence: persist one validated, non-binding quotation draft for a verified actor.

    Principal + stored RFQ + trusted evaluation inputs
        -> create_quote_draft -> quotes / quote_versions / quote_line_items
           + idempotency receipt + audit event, in one SAVEPOINT of the caller's transaction

Trust: nothing an agent produced is persisted. The draft is recomputed here, inside the
write transaction, by ``services.evaluation.evaluate_stored_rfq`` under the server-pinned,
manifest-verified rule versions (``mfg-v1.0``, ``price-v1.0``, ``approval-v1.2``,
``workflow-v1.1``). Supervisor or specialist evidence is never read, so its unauthenticated
durable form (L04/L21/L26) cannot become business authority. A caller may pass the
``evaluation_fingerprint`` it reviewed; a different recomputed result is refused.

Authorization does not trust the Principal object: the actor must be an active user whose
role is granted ``create_quote_draft`` and who holds an eligible binding to the Principal's
customer, all re-read in this transaction. The RFQ must belong to that customer (and the
assigned user, when assignments are enforced); a foreign RFQ is ``not_found``.

Idempotency: a key is required. The receipt is unique per (actor, customer, operation, key)
and ``quotes.rfq_id`` is unique, so PostgreSQL, not a process lock, decides concurrent
races. Same key and same request: the stored receipt is returned (``replayed``). Same key,
different request: ``idempotency_key_conflict``. Another key for an RFQ that already has a
quote: ``quote_exists``. Races are classified only by the violated constraint's name
(``uq_quotes_rfq_id``, the receipt key); any other integrity error propagates. Receipts are
written in the same transaction as the draft, so a rollback never leaves a receipt without
its draft.

Insert order: ``quotes.(quote_id, current_version)`` references ``quote_versions`` through
the only deferrable foreign key, while ``quote_versions.quote_id`` references ``quotes``
immediately. Inside the savepoint that one pointer is deferred, the quote and version 1 are
inserted, and the pointer is checked immediately again before anything else is written.

Drafts are ``status='draft'``, version 1, ``approval_valid=False``, never submitted,
approved or released. integrity (K3): new drafts record ``workflow-v1.2``, whose
``quote_validity`` makes version 1 ``complete``. Inside the savepoint the service inserts
(or reuses, verified column by column) the RFQ's calculation record, then the version with
its evidence columns and ``expiry_date`` (UTC creation date + validity - 1, from the pinned
rule), then its lines, and forces the deferred line-completeness triggers IMMEDIATE before
anything else is written. A workflow rule without validity (v1.0/v1.1, set only by tests or
a revision of a legacy quotation) still produces a ``priced_partial`` version with no K3
evidence. All creation times are PostgreSQL's ``transaction_timestamp()``, never the app
clock. ``complete`` means the deterministic financial and provenance record is internally
complete; it does not mean the RFQ source or the evaluation context are authenticated (54C).
Internal cost breakdowns go only to ``quote_line_items.cost_breakdown_json`` and
``pricing_calculations.lines``; the returned receipt, the stored receipt and the audit
event carry no cost or margin.

The service never commits and never logs; the caller owns the transaction.
"""

import hmac
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from rfq_copilot import quote_evidence as k3
from rfq_copilot.api.auth import Principal
from rfq_copilot.canonical import fingerprint
from rfq_copilot.mcp.contracts import NEW_EVALUATION_APPROVAL_RULE_VERSION
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION, PRICING_RULE_VERSION, pinned_rules
from rfq_copilot.models import AuditEvent, Quote, QuoteLineItem, QuoteVersion, User
from rfq_copilot.repositories import eligible_customers, get_rfq, granted_permissions
from rfq_copilot.repositories.idempotency import add_receipt, find_receipt
from rfq_copilot.repositories.queries import _identifier
from rfq_copilot.services.evaluation import RfqEvaluation, evaluate_stored_rfq
from rfq_copilot.services.pricing import canonical_part, display_quote

OPERATION = "create_quote_draft"  # idempotency_records rule_01 and the dataset permission
# New drafts only. A revision keeps the workflow rule its quotation already records, unless
# it is an explicit one-way adoption of this rule (quote_versions, integrity R8).
WORKFLOW_RULE_VERSION = "workflow-v1.2"
AUDIT_OPERATION = "quote_draft.create"
REQUEST_VERSION = "create-quote-draft-v1"
# Constraint names from revision 20261108_01 (naming convention in models/base.py).
# quotes -> quote_versions closes the cycle and is DEFERRABLE INITIALLY DEFERRED;
# quote_versions -> quotes is immediate, so the version can never be inserted first.
CURRENT_VERSION_FK = "business.fk_quotes_quote_id_current_version_quote_versions"
QUOTE_PER_RFQ = "uq_quotes_rfq_id"
# The declared name (op.f, 69 characters) exceeds PostgreSQL's 63, so SQLAlchemy stored it
# truncated with an md5 suffix (database verification verification evidence); 23505 errors name this one.
RECEIPT_KEY = "uq_idempotency_records_actor_id_customer_id_operation_i_fd7c"
_SHA256 = re.compile(r"[0-9a-f]{64}")

Reason = Literal[
    "invalid_request",
    "permission_denied",
    "not_found",
    "quote_exists",
    "not_draftable",
    "evaluation_changed",
    "invalid_evaluation",
    "idempotency_key_conflict",
    # versioning revisions (rfq_copilot.quote_versions)
    "stale_version",
    "revision_not_allowed",
    "rule_version_changed",
    "provenance_missing",
    "unchanged",
    # approval policy approval-policy assessments (rfq_copilot.quote_approval_policy)
    "not_assessable",
    "version_inconsistent",
    # integrity K3: a stored calculation record under this key holds other content
    "calculation_record_mismatch",
    # integrity human decisions (rfq_copilot.quote_approval)
    "not_submittable",
    "not_pending",
    "separation_of_duties",
    "attestation_mismatch",
    "review_unverified",
    "quote_expired",
    "not_approvable",
    # human decision lifecycle (rfq_copilot.quote_approval)
    "not_releasable",
    "resubmission_blocked",
]


class DraftRefused(Exception):
    """A refusal whose ``reason`` is fixed vocabulary, safe to publish. Nothing was written."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class QuoteDraft:
    """What a persisted draft promises: selling total and provenance, no cost or margin."""

    quote_id: str
    rfq_id: str
    customer_id: str
    version: int
    status: Literal["draft", "revised_draft"]
    currency: str
    total_usd: Decimal
    calculation_id: str
    evaluation_fingerprint: str
    outcome: str
    manager_review_required: bool
    manager_review_reasons: tuple[str, ...]
    rule_versions: tuple[str, ...]  # manufacturing, pricing, approval, workflow
    # K3: the inclusive last valid date (customer-facing); None for a partial version.
    expiry_date: date | None = None
    human_approval_required: Literal[True] = True
    approved: Literal[False] = False
    released: Literal[False] = False
    replayed: bool = False

    def to_json(self) -> dict:
        data = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "replayed"}
        data.update(
            total_usd=str(self.total_usd),
            manager_review_reasons=list(self.manager_review_reasons),
            rule_versions=list(self.rule_versions),
            expiry_date=None if self.expiry_date is None else self.expiry_date.isoformat(),
        )
        return data

    @classmethod
    def from_json(cls, data: dict) -> "QuoteDraft":
        expiry = data.get("expiry_date")  # absent from receipts written before K3
        return cls(
            **{
                **data,
                "total_usd": Decimal(data["total_usd"]),
                "manager_review_reasons": tuple(data["manager_review_reasons"]),
                "rule_versions": tuple(data["rule_versions"]),
                "expiry_date": None if expiry is None else date.fromisoformat(expiry),
                "replayed": True,
            }
        )


def request_digest(principal: Principal, rfq_id: str, inputs: dict) -> str:
    """What one idempotency key promises: same customer, RFQ and evaluation inputs."""
    return fingerprint(
        {
            "request_version": REQUEST_VERSION,
            "customer_id": principal.customer_id,
            "rfq_id": rfq_id,
            **inputs,
        }
    )


def _valid_request(principal, rfq_id, key, correlation_id, inputs) -> None:
    try:
        if not isinstance(principal, Principal) or principal.user_id is None:
            raise ValueError
        _identifier(principal.user_id)
        _identifier(rfq_id)
        if not isinstance(key, str) or not 0 < len(key) <= 256 or not key.isascii():
            raise ValueError
        if not key.isprintable() or key != key.strip():
            raise ValueError
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise ValueError
        horizon = inputs["horizon_start"]
        if horizon is not None and type(horizon) is not date:  # None: server-owned (54C)
            raise ValueError
        delivery = inputs["requested_delivery_date"]
        if delivery is not None and type(delivery) is not date:
            raise ValueError
        if not all(isinstance(c, str) and c.strip() for c in inputs["contradictions"]):
            raise ValueError
        for name in ("inventory_snapshot", "capacity_snapshot"):
            if inputs[name] is not None:
                _identifier(inputs[name])
        expected = inputs["expected_evaluation_fingerprint"]
        if expected is not None and not (isinstance(expected, str) and _SHA256.fullmatch(expected)):
            raise ValueError
    except ValueError:
        raise DraftRefused("invalid_request") from None


async def _authorize(
    session: AsyncSession, principal: Principal, permission: str = OPERATION
) -> None:
    """Re-read the actor, grant and customer binding; never trust the Principal's copy."""
    user = await session.scalar(
        select(User)
        .where(User.user_id == principal.user_id, User.active.is_(True))
        .options(raiseload("*"))
        # A User already in the identity map would keep a stale role (and so stale grants).
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or permission not in await granted_permissions(session, user)
        or principal.customer_id not in await eligible_customers(session, user)
    ):
        raise DraftRefused("permission_denied")


async def _replay(
    session, principal: Principal, key: str, digest: str, operation=OPERATION, result=None
):
    """This actor's receipt for the key as ``result`` (QuoteDraft), a conflict, or None."""
    receipt = await find_receipt(
        session,
        actor_id=principal.user_id,
        customer_id=principal.customer_id,
        key=key,
        operation=operation,
    )
    if receipt is None:
        return None
    if not hmac.compare_digest(receipt.request_sha256, digest):
        raise DraftRefused("idempotency_key_conflict")
    return (result or QuoteDraft).from_json(receipt.result_json)


def violated_unique(error: IntegrityError) -> str | None:
    """The unique constraint a 23505 named (asyncpg), else None: never guessed from text."""
    adapted = error.orig
    if getattr(adapted, "sqlstate", None) != "23505":
        return None
    return getattr(getattr(adapted, "orig", None), "constraint_name", None)


def _workflow_allows_drafts(workflow) -> None:
    document = workflow.document
    writes = document.get("idempotent_writes", ())
    if OPERATION not in writes or "draft" not in document.get("transitions", {}):
        raise DraftRefused("invalid_evaluation")


def _checked(evaluation: RfqEvaluation, principal: Principal, expected: str | None):
    """The quotation and pricing of a draftable evaluation, or a refusal."""
    if not evaluation.draft_creation_allowed:
        raise DraftRefused("not_draftable")
    if expected is not None and not hmac.compare_digest(
        evaluation.evaluation_fingerprint, expected
    ):
        raise DraftRefused("evaluation_changed")
    quotation = evaluation.quotation
    pricing = quotation.pricing if quotation is not None else None
    if (
        quotation is None
        or pricing is None
        or evaluation.human_release_approval_required is not True
        or evaluation.approval_must_be_human is not True
        or evaluation.quote_release_authorized_for_agent is not False
        or evaluation.customer.customer_id != principal.customer_id
        or quotation.result != "quoted"
        or pricing.result != "priced"
        or pricing.currency != "USD"
        or quotation.lead_time.snapshots is None
        or not isinstance(quotation.quote_total_usd, Decimal)
        or not quotation.quote_total_usd.is_finite()
        or quotation.quote_total_usd < 0
        or len(pricing.lines) != len(quotation.parts)
        or any(p.line_total_usd < 0 or p.cost.quantity <= 0 for p in pricing.lines)
        or sum(p.line_total_usd for p in pricing.lines) != quotation.quote_total_usd
        or [p.cost.part_id for p in pricing.lines] != [p.part_id for p in quotation.parts]
    ):
        raise DraftRefused("invalid_evaluation")
    return quotation, pricing


async def pinned_evaluation_rules(session) -> dict:
    """The server-pinned, manifest-verified evaluation rules, as evaluate_stored_rfq kwargs."""
    return {
        "manufacturing_rules": await pinned_rules(session, MANUFACTURING_RULE_VERSION),
        "pricing_rules": await pinned_rules(session, PRICING_RULE_VERSION),
        "approval_rules": await pinned_rules(session, NEW_EVALUATION_APPROVAL_RULE_VERSION),
    }


async def _evaluate(
    session, principal: Principal, rfq_id: str, inputs: dict, workflow_version: str | None = None
):
    """The trusted evaluation under the server pins: (rules, workflow, evaluation,
    quotation, pricing). Shared by drafts and revisions; refusals as in ``_checked``.
    ``workflow_version`` (revisions: the quotation's recorded rule) must be a manifest
    workflow entry; ``pinned_rules`` re-verifies the stored row against it."""
    rules = await pinned_evaluation_rules(session)
    workflow = await pinned_rules(session, workflow_version or WORKFLOW_RULE_VERSION)
    _workflow_allows_drafts(workflow)
    evaluation = await evaluate_stored_rfq(
        session,
        rfq_id,
        customer_id=principal.customer_id,
        **rules,
        horizon_start=inputs["horizon_start"],
        requested_delivery_date=inputs["requested_delivery_date"],
        contradictions=inputs["contradictions"],
        inventory_snapshot=inputs["inventory_snapshot"],
        capacity_snapshot=inputs["capacity_snapshot"],
    )
    if evaluation is None:
        raise DraftRefused("not_found")
    quotation, pricing = _checked(evaluation, principal, inputs["expected_evaluation_fingerprint"])
    return rules, workflow, evaluation, quotation, pricing


def rule_versions(rules: dict, workflow) -> tuple[str, ...]:
    """manufacturing, pricing, approval, workflow: the order of QuoteDraft.rule_versions."""
    return (*(r.rule_version for r in rules.values()), workflow.rule_version)


def snapshot_pins(quotation) -> dict:
    snapshots = quotation.lead_time.snapshots
    return {
        "inventory": snapshots.inventory.snapshot_id,
        "capacity": snapshots.capacity.snapshot_id,
        "dataset_version": snapshots.inventory.dataset_version,
    }


async def complete_evidence(
    session, actor, rfq_id, customer, details, now, rules, workflow, evaluation, quotation,
    pricing, inputs,
) -> dict | None:  # fmt: skip
    """K3: the calculation record and evidence of a complete version, or None when the
    workflow rule states no validity (a partial version). Call inside the savepoint: the
    record row is written (or reused) here."""
    dates = k3.validity_dates(workflow)
    if dates is None:
        return None
    rfq_source = k3.rfq_source_sha256(rfq_id, customer, details.runtime_lines)
    row = k3.record_row(
        rfq_id=rfq_id, actor=actor, now=now, rfq_source=rfq_source, parts=quotation.parts,
        pricing=pricing, pricing_rules=rules["pricing_rules"], snapshots=snapshot_pins(quotation),
    )  # fmt: skip
    record = await k3.calculation_record(session, row)
    if record is None:
        raise DraftRefused("calculation_record_mismatch")
    return k3.version_evidence(
        rfq_id=rfq_id, record=record, rfq_source=rfq_source, evaluation=evaluation, rules=rules,
        workflow=workflow, inputs=inputs, now=now, dates=dates,
    )  # fmt: skip


def expected_expiry(workflow, now) -> date | None:
    dates = k3.validity_dates(workflow)
    return None if dates is None else k3.expiry_date(now, dates)


def version_rows(
    quote_id, number, actor, now, rules, workflow, evaluation, quotation, pricing, evidence=None
):
    """One immutable version snapshot and its line items (internal costs only here).
    ``evidence`` (``complete_evidence``) makes it complete; None keeps it priced_partial."""
    evidence = dict(evidence or {})
    complete = bool(evidence)
    version = QuoteVersion(
        quote_version_id=f"{quote_id}-V{number}",
        quote_id=quote_id,
        version=number,
        created_by=actor,
        created_at=now,
        is_current=True,
        approval_valid=False,
        currency=pricing.currency,
        total_usd=quotation.quote_total_usd,
        pricing_rule_version=pricing.pricing_rule_version,
        manufacturing_rule_version=rules["manufacturing_rules"].rule_version,
        approval_rule_version=evaluation.approval_rule_version,
        workflow_rule_version=workflow.rule_version,
        expiry_date=evidence.pop("expiry_date", None),
        estimated_lead_business_days=quotation.lead_business_days,
        source_snapshot_versions=snapshot_pins(quotation),
        calculation_id=evidence.pop("calculation_id", None),  # set only with its record
        snapshot_completeness=k3.COMPLETE if complete else "priced_partial",
        **evidence,  # the K3 columns (rule_07/rule_09: all or none)
    )
    breakdown = display_quote(pricing, rules["pricing_rules"])["lines"]
    lines = [
        QuoteLineItem(
            quote_line_id=f"{quote_id}-V{number}-L{line_number}",
            quote_id=quote_id,
            quote_version=number,
            line_number=line_number,
            part_id=part.part_id,
            part_json=canonical_part(part),
            cost_breakdown_json=cost,  # internal only; never returned
            quantity=priced.cost.quantity,
            line_total_usd=priced.line_total_usd,
        )
        for line_number, part, priced, cost in zip(
            quotation.line_numbers, quotation.parts, pricing.lines, breakdown, strict=True
        )
    ]
    return version, lines


def event_data(result: QuoteDraft, principal: Principal, inputs: dict) -> dict:
    """Audit facts: provenance and the caller-supplied context; never cost or RFQ text."""
    delivery = inputs["requested_delivery_date"]
    return {
        "evaluation_fingerprint": result.evaluation_fingerprint,
        "calculation_id": result.calculation_id,
        "outcome": result.outcome,
        "rule_versions": list(result.rule_versions),
        # Contradiction text may quote the customer, so only the count.
        "horizon_start": inputs["horizon_start"].isoformat(),
        "requested_delivery_date": delivery.isoformat() if delivery else None,
        "contradiction_count": len(inputs["contradictions"]),
        "assignment_enforced": principal.assignment_required,
    }


async def create_quote_draft(
    session: AsyncSession,
    principal: Principal,
    rfq_id: str,
    *,
    idempotency_key: str,
    correlation_id: str,
    horizon_start: date | None = None,
    requested_delivery_date: date | None = None,
    contradictions: Sequence[str] = (),
    inventory_snapshot: str | None = None,
    capacity_snapshot: str | None = None,
    expected_evaluation_fingerprint: str | None = None,
) -> QuoteDraft:
    """Persist (flush, not commit) one draft, or replay this key's earlier draft.
    ``horizon_start`` None (54C) records the server-owned horizon of the creation week; a
    caller-dated horizon is kept as legacy context and never counts as authenticated."""
    if isinstance(contradictions, str):
        raise DraftRefused("invalid_request")  # a list of findings, never one string
    inputs = {
        "horizon_start": horizon_start,
        "requested_delivery_date": requested_delivery_date,
        "contradictions": tuple(contradictions),
        "inventory_snapshot": inventory_snapshot,
        "capacity_snapshot": capacity_snapshot,
        "expected_evaluation_fingerprint": expected_evaluation_fingerprint,
    }
    _valid_request(principal, rfq_id, idempotency_key, correlation_id, inputs)
    digest = request_digest(principal, rfq_id, inputs)
    customer, key = principal.customer_id, idempotency_key

    await _authorize(session, principal)
    details = await get_rfq(
        session, rfq_id, customer_id=customer, assigned_user=principal.assigned_user
    )
    if details is None:
        raise DraftRefused("not_found")  # absent, foreign or unassigned: identical
    replay = await _replay(session, principal, key, digest)
    if replay is not None:
        return replay
    if await session.scalar(select(Quote.quote_id).where(Quote.rfq_id == rfq_id)) is not None:
        raise DraftRefused("quote_exists")
    if not details.runtime_lines:
        raise DraftRefused("not_draftable")  # nothing evaluable, e.g. a historical RFQ

    now = await k3.database_now(session)  # one trusted creation time (K3, R3)
    if inputs["horizon_start"] is None:  # 54C: the server, not the caller, dates the context
        inputs = {**inputs, "horizon_start": k3.server_horizon(now)}
    rules, workflow, evaluation, quotation, pricing = await _evaluate(
        session, principal, rfq_id, inputs
    )

    quote_id = f"QUOTE-{secrets.token_hex(16).upper()}"  # 38 characters, 128 random bits
    versions = rule_versions(rules, workflow)
    draft = QuoteDraft(
        quote_id=quote_id,
        rfq_id=rfq_id,
        customer_id=customer,
        version=1,
        status="draft",
        currency=pricing.currency,
        total_usd=quotation.quote_total_usd,
        calculation_id=pricing.calculation_id,
        evaluation_fingerprint=evaluation.evaluation_fingerprint,
        outcome=evaluation.outcome,
        manager_review_required=evaluation.manager_review_required,
        manager_review_reasons=evaluation.manager_review_reasons,
        rule_versions=versions,
        expiry_date=expected_expiry(workflow, now),
    )
    try:
        async with session.begin_nested():  # any failure undoes every row below
            # The caller's transaction may have made the deferrable pointer immediate (the
            # seed loader runs SET CONSTRAINTS ALL IMMEDIATE). Defer only that pointer and
            # the K3 line-completeness triggers while the quote, its first version and its
            # lines are inserted, then check each before going on, so a violation is raised
            # here, inside the savepoint, never at the caller's COMMIT. They are left
            # IMMEDIATE (never weaker than the schema).
            await session.execute(
                text(f"SET CONSTRAINTS {CURRENT_VERSION_FK}, {k3.LINE_CONSTRAINTS} DEFERRED")
            )
            evidence = await complete_evidence(
                session, principal.user_id, rfq_id, customer, details, now, rules, workflow,
                evaluation, quotation, pricing, inputs,
            )  # fmt: skip
            version, lines = version_rows(
                quote_id, 1, principal.user_id, now, rules, workflow, evaluation, quotation,
                pricing, evidence,
            )  # fmt: skip
            session.add(
                Quote(
                    quote_id=quote_id,
                    rfq_id=rfq_id,
                    current_version=1,
                    status="draft",
                    created_by=principal.user_id,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(version)
            await session.flush()
            await session.execute(text(f"SET CONSTRAINTS {CURRENT_VERSION_FK} IMMEDIATE"))
            session.add_all(lines)
            await session.flush()
            await session.execute(text(f"SET CONSTRAINTS {k3.LINE_CONSTRAINTS} IMMEDIATE"))
            session.add(
                AuditEvent(
                    actor_id=principal.user_id,
                    customer_id=customer,
                    rfq_id=rfq_id,
                    quote_id=quote_id,
                    quote_version=1,
                    operation=AUDIT_OPERATION,
                    outcome="success",
                    correlation_id=correlation_id,
                    event_data=event_data(draft, principal, inputs),
                )
            )
            add_receipt(
                session,
                actor_id=principal.user_id,
                customer_id=customer,
                key=key,
                request_sha256=digest,
                result=draft.to_json(),
                correlation_id=correlation_id,
                operation=OPERATION,
            )
            await session.flush()
    except IntegrityError as error:
        # The savepoint is rolled back. Only the two identity constraints mean "another
        # request won": replay its receipt for this key, or name the conflict. Any other
        # violation (foreign key, CHECK, NOT NULL, another unique) propagates unchanged.
        constraint = violated_unique(error)
        if constraint not in (QUOTE_PER_RFQ, RECEIPT_KEY):
            raise
        replay = await _replay(session, principal, key, digest)  # may raise key conflict
        if replay is not None:
            return replay
        if constraint == QUOTE_PER_RFQ:
            raise DraftRefused("quote_exists") from None
        raise
    return draft
