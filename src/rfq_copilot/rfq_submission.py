"""Customer RFQ creation (Milestone 2 Step 2): the service behind ``POST /api/v1/rfqs``.

- Identity: the actor, its ``customer`` role, the ``customer_create_rfq`` grant and the
  customer binding are re-read in the caller's write transaction; the Principal's copy is
  never trusted. The customer and creator come only from there, never from the body.
- Idempotency (``business.idempotency_records``, operation ``create_rfq``): a receipt per
  (actor, customer, operation, key). Same key and request: the stored receipt is replayed.
  Same key, other request: ``idempotency_key_conflict``. Two concurrent first attempts
  both reach the INSERTs and the receipt's unique constraint decides; the loser replays.
- Atomicity: receipt, RFQ, line items and the ``rfq.create`` audit event are written in one
  savepoint; any failure undoes all of them. Callers own the session and commit.
- Checkpoint A1 (``enqueue=True``, only with ``RFQ_API_WORKFLOW_QUEUE=enabled``): the same
  savepoint also adds exactly one ``queued`` workflow run and its ``workflow.enqueue``
  event (``workflow_queue.enqueue``), so there is no committed RFQ without its processing
  request. A replay returns the stored receipt and enqueues nothing. Without the recorded
  queue activation the submission is refused (``queue_not_activated``), never stored bare.
- Nothing runs here: no Supervisor, no evaluation, no quote. The RFQ status stays the
  schema's ``draft`` default (RFQ rows are immutable); processing state lives on the run.
"""

import hashlib
import hmac
import json
import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from rfq_copilot import workflow_queue
from rfq_copilot.api.auth import CUSTOMER_CREATE_RFQ, CUSTOMER_ROLE, Principal
from rfq_copilot.api.schemas.rfq import RfqReceipt, RfqSubmissionRequest
from rfq_copilot.models import RFQ, AuditEvent, Finish, Material, RFQLineItem, User
from rfq_copilot.quote_drafts import RECEIPT_KEY, violated_unique
from rfq_copilot.repositories.idempotency import add_receipt, find_receipt
from rfq_copilot.repositories.identity import eligible_customers, granted_permissions

OPERATION = "create_rfq"  # idempotency_records rule_01 (initial schema)
AUDIT_OPERATION = "rfq.create"
SOURCE_TYPE = "customer_portal"  # runtime RFQs; seeded ones are historical_internal
REQUEST_VERSION = "create-rfq-v1"
# 23514 CHECK (e.g. bend_radius_mm = 0, which rfq_line_items rule_02 forbids), 23503 a
# material or finish removed after the pre-check: the request is refused, nothing written.
_REQUEST_VIOLATIONS = frozenset({"23514", "23503"})

Reason = Literal[
    "permission_denied",
    "idempotency_key_conflict",
    "unknown_material",
    "unknown_finish",
    "delivery_date_not_stored",
    "rejected_by_constraints",
    "queue_not_activated",
]


class RfqRefused(Exception):
    """A refusal with a fixed-vocabulary ``reason`` (and part index); nothing was written."""

    def __init__(self, reason: Reason, index: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.index = index


def request_digest(customer_id: str, request: RfqSubmissionRequest) -> str:
    canonical = json.dumps(
        [REQUEST_VERSION, customer_id, request.model_dump(mode="json")],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _authorize(session: AsyncSession, principal: Principal) -> str:
    """The verified actor's user_id: active, role ``customer``, granted, bound to the customer."""
    if principal.user_id is None:
        raise RfqRefused("permission_denied")
    user = await session.scalar(
        select(User)
        .where(User.user_id == principal.user_id, User.active.is_(True))
        .options(raiseload("*"))
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or user.role != CUSTOMER_ROLE
        or CUSTOMER_CREATE_RFQ not in await granted_permissions(session, user)
        or principal.customer_id not in await eligible_customers(session, user)
    ):
        raise RfqRefused("permission_denied")
    return user.user_id


async def _replay(session, actor: str, customer: str, key: str, digest: str):
    receipt = await find_receipt(
        session, actor_id=actor, customer_id=customer, key=key, operation=OPERATION
    )
    if receipt is None:
        return None
    if not hmac.compare_digest(receipt.request_sha256, digest):
        raise RfqRefused("idempotency_key_conflict")
    return RfqReceipt.model_validate(receipt.result_json)


async def _check_references(session, request: RfqSubmissionRequest) -> None:
    """Refuse the first part naming a material or finish that is not in the catalog."""
    for column, model, reason in (
        ("material_id", Material, "unknown_material"),
        ("finish_id", Finish, "unknown_finish"),
    ):
        wanted = {getattr(p, column) for p in request.parts} - {None}
        if not wanted:
            continue
        key = getattr(model, column)
        known = set((await session.scalars(select(key).where(key.in_(wanted)))).all())
        for index, part in enumerate(request.parts):
            if getattr(part, column) is not None and getattr(part, column) not in known:
                raise RfqRefused(reason, index)


async def create_customer_rfq(
    session: AsyncSession,
    principal: Principal,
    request: RfqSubmissionRequest,
    key: str,
    *,
    enqueue: bool = False,
) -> tuple[RfqReceipt, bool]:
    """Store the RFQ, or replay this key's receipt. Returns (receipt, replayed)."""
    actor = await _authorize(session, principal)
    customer = principal.customer_id  # verified against the binding by _authorize
    if request.requested_delivery_date is not None:
        # business.rfqs has no delivery-date column; refusing beats silently dropping it.
        raise RfqRefused("delivery_date_not_stored")
    digest = request_digest(customer, request)
    replay = await _replay(session, actor, customer, key, digest)
    if replay is not None:
        return replay, True
    await _check_references(session, request)
    if enqueue and not await workflow_queue.queue_activated(session):
        raise RfqRefused("queue_not_activated")

    rfq_id = f"RFQ-{uuid.uuid4().hex}"
    receipt = RfqReceipt(rfq_id=rfq_id, status="draft")
    try:
        async with session.begin_nested():  # any failure undoes every row below
            # The receipt first: a concurrent first attempt with this key waits here on the
            # unique index, then fails before writing anything else.
            add_receipt(
                session, actor_id=actor, customer_id=customer, key=key, request_sha256=digest,
                result=receipt.model_dump(mode="json"), correlation_id=rfq_id,
                operation=OPERATION,
            )  # fmt: skip
            await session.flush()
            session.add(
                RFQ(
                    rfq_id=rfq_id,
                    customer_id=customer,
                    raw_request=request.raw_request,
                    source_type=SOURCE_TYPE,
                    part_ids=[p.part_id for p in request.parts],
                    status="draft",
                    created_by=actor,
                )
            )
            await session.flush()
            session.add_all(
                RFQLineItem(rfq_id=rfq_id, line_number=number, **part.model_dump())
                for number, part in enumerate(request.parts, start=1)
            )
            await session.flush()
            session.add(
                AuditEvent(
                    actor_id=actor,
                    customer_id=customer,
                    rfq_id=rfq_id,
                    operation=AUDIT_OPERATION,
                    outcome="success",
                    correlation_id=rfq_id,
                    # Counts and provenance only: no raw request, part data or credentials.
                    event_data={"source_type": SOURCE_TYPE, "line_count": len(request.parts)},
                )
            )
            await session.flush()
            if enqueue:
                workflow_queue.enqueue(session, rfq_id=rfq_id, customer_id=customer, actor=actor)
                await session.flush()
    except IntegrityError as error:
        if violated_unique(error) == RECEIPT_KEY:  # another attempt with this key won
            replay = await _replay(session, actor, customer, key, digest)
            if replay is not None:
                return replay, True
            raise
        if getattr(error.orig, "sqlstate", None) in _REQUEST_VIOLATIONS:
            raise RfqRefused("rejected_by_constraints") from None
        raise
    return receipt, False
