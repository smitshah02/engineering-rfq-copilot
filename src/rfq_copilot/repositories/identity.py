"""Identity lookups for bearer authentication. Callers own sessions; reads only.

``business.users.authentication_subject`` (unique) links an external identity to a user.
``business.user_customer_bindings`` (approved TD-012, revision 20261110_01) is the only
source of the customer a user acts as, and ``business.role_permissions`` (through\n``users.role``) the only source of what the user may do. Nothing is inferred from email, name, region, RFQ
authorship or token claims.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from rfq_copilot.models import Customer, RolePermission, User, UserCustomerBinding

# customers.account_status values (CHECK rule_01) that may authenticate. Status rules for
# commercial review and release stay in the business services. 'inactive', and any value
# not listed here, cannot sign in.
ELIGIBLE_ACCOUNT_STATUSES = frozenset(
    {"active", "pending_verification", "on_hold", "credit_restricted"}
)


async def find_active_user(session: AsyncSession, subject: str) -> User | None:
    """The active user whose ``authentication_subject`` equals ``subject`` exactly."""
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
        raise ValueError("subject must be nonblank text of at most 255 characters.")
    statement = (
        select(User)
        .where(User.authentication_subject == subject, User.active.is_(True))
        .options(raiseload("*"))
    )
    with session.no_autoflush:
        return await session.scalar(statement)


async def customer_binding(session: AsyncSession, user: User) -> str | None:
    """The one customer the verified user is explicitly bound to, or ``None``.

    Requires an unrevoked binding row for ``user.user_id`` whose customer has an eligible
    ``account_status``. No row, a revoked row, an ineligible customer, or more than one
    match (impossible under the partial unique index; checked anyway) all fail closed.
    """
    statement = (
        select(UserCustomerBinding.customer_id)
        .join(Customer, Customer.customer_id == UserCustomerBinding.customer_id)
        .where(
            UserCustomerBinding.user_id == user.user_id,
            UserCustomerBinding.revoked_at.is_(None),
            Customer.account_status.in_(sorted(ELIGIBLE_ACCOUNT_STATUSES)),
        )
        .limit(2)
    )
    with session.no_autoflush:
        rows = (await session.scalars(statement)).all()
    return rows[0] if len(rows) == 1 else None


async def eligible_customers(session: AsyncSession, user: User) -> tuple[str, ...]:
    """Every customer the user holds an unrevoked binding to, whose status is eligible.

    For explicit customer context (deferred revision 20261111_05). Ordered; at most 64.
    """
    statement = (
        select(UserCustomerBinding.customer_id)
        .join(Customer, Customer.customer_id == UserCustomerBinding.customer_id)
        .where(
            UserCustomerBinding.user_id == user.user_id,
            UserCustomerBinding.revoked_at.is_(None),
            Customer.account_status.in_(sorted(ELIGIBLE_ACCOUNT_STATUSES)),
        )
        .order_by(UserCustomerBinding.customer_id)
        .limit(64)
    )
    with session.no_autoflush:
        return tuple((await session.scalars(statement)).all())


async def token_revoked(
    session: AsyncSession, user: User, token_sha256: str, issued_at: int
) -> bool:
    """True if this exact token, or every token of the user issued before a cut-off, is
    revoked in ``business.token_revocations`` (revision 20261111_03). A missing table
    raises, so the caller fails closed."""
    from datetime import UTC, datetime

    from sqlalchemy import exists, or_

    from rfq_copilot.models import TokenRevocation

    revoked = TokenRevocation.__table__

    issued = datetime.fromtimestamp(issued_at, UTC)
    condition = exists().where(
        revoked.c.user_id == user.user_id,
        or_(revoked.c.token_sha256 == token_sha256, revoked.c.not_before > issued),
    )
    with session.no_autoflush:
        return bool(await session.scalar(select(condition)))


async def granted_permissions(session: AsyncSession, user: User) -> frozenset[str]:
    """The permissions ``business.role_permissions`` grants ``user.role`` (``allowed`` only).

    Read on every request, so a changed grant or role applies to the next request. A role
    with no rows, or only ``allowed = false`` rows, has no permissions.
    """
    statement = select(RolePermission.permission).where(
        RolePermission.role == user.role, RolePermission.allowed.is_(True)
    )
    with session.no_autoflush:
        return frozenset((await session.scalars(statement)).all())
