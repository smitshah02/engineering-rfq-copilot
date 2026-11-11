"""Upload idempotency receipts in ``business.idempotency_records`` (revision 20261111_04).

Receipts are scoped by (actor, customer, operation, key), the table's unique constraint,
so one customer's key never replays another's result. Callers own sessions and commits;
the receipt is inserted in the same transaction as the attachment row.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from rfq_copilot.models import IdempotencyRecord
from rfq_copilot.repositories.queries import _identifier

UPLOAD_OPERATION = "attach_rfq_document"


async def find_receipt(
    session: AsyncSession, *, actor_id: str, customer_id: str, key: str
) -> IdempotencyRecord | None:
    _identifier(actor_id)
    _identifier(customer_id)
    statement = (
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.customer_id == customer_id,
            IdempotencyRecord.operation == UPLOAD_OPERATION,
            IdempotencyRecord.idempotency_key == key,
        )
        .options(raiseload("*"))
    )
    with session.no_autoflush:
        return await session.scalar(statement)


def add_receipt(
    session: AsyncSession,
    *,
    actor_id: str,
    customer_id: str,
    key: str,
    request_sha256: str,
    result: dict,
    correlation_id: str,
) -> IdempotencyRecord:
    row = IdempotencyRecord(
        receipt_id=uuid.uuid4(),
        actor_id=actor_id,
        customer_id=customer_id,
        operation=UPLOAD_OPERATION,
        idempotency_key=key,
        request_sha256=request_sha256,
        result_json=result,
        correlation_id=correlation_id,
    )
    session.add(row)
    return row
