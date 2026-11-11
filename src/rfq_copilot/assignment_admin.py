"""Operator-only, audited RFQ staff assignments (``business.rfq_assignments``).

    python -m rfq_copilot.assignment_admin grant  --user U --rfq R --operator O --reason onboarding [--execute]
    python -m rfq_copilot.assignment_admin revoke --user U --rfq R --operator O --reason offboarding [--execute]
    python -m rfq_copilot.assignment_admin list   (--user U | --rfq R)
    python -m rfq_copilot.assignment_admin verify [--user U]

Assignments only narrow access (RFQ_API_RFQ_ASSIGNMENTS=required), never widen it.
- Dry run unless ``--execute``; one transaction with an ``audit_events`` row; refused
  executions are audited separately. Nothing is generated automatically.
- ``--operator`` must be an active user whose role is granted ``manage_customer_bindings``
  (the identity-administration authority; only ``identity_operator`` holds it), and
  must not be the assignee.
- Grant validates: the user is active; the RFQ exists; the user holds an active,
  eligible binding to the RFQ's customer (assignments never cross customers); the user's
  role holds an RFQ-scoped permission; an existing active assignment is a no-op and a
  revoked one is reactivated. Concurrent grants are serialized by row locks and the
  primary key; a lost race is refused, not duplicated.
- ``verify`` reports active assignments that no longer line up (inactive user, or no
  active binding to the RFQ's customer). It never changes anything.
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from rfq_copilot.identity_admin import (
    BINDING_PERMISSION,
    REASONS,
    Outcome,
    Refused,
    _active_user,
    _audit,
    _authorize,
    _privileged,
)
from rfq_copilot.models import RFQ, RfqAssignment, RolePermission, User, UserCustomerBinding
from rfq_copilot.repositories.identity import eligible_customers
from rfq_copilot.repositories.queries import _identifier

RFQ_SCOPED_PERMISSIONS = ("view_assigned_rfqs", "view_quote", "attach_rfq_document", "create_rfq")
OPERATIONS = {"grant": "rfq_assignment.grant", "revoke": "rfq_assignment.revoke"}


async def _rfq_customer(session, rfq_id: str) -> str:
    _identifier(rfq_id)
    customer = await session.scalar(select(RFQ.customer_id).where(RFQ.rfq_id == rfq_id))
    if customer is None:
        raise Refused("rfq_unknown")
    return customer


async def grant_assignment(session, *, operator, user, rfq, reason, execute) -> Outcome:
    if not await _privileged(session, "rfq_assignments", "INSERT,UPDATE") or not await _privileged(session, "audit_events", "INSERT"):  # fmt: skip
        raise Refused("database_role_lacks_privileges")
    await _authorize(session, operator, BINDING_PERMISSION, user)
    await _active_user(session, user)
    customer = await _rfq_customer(session, rfq)
    target = await session.scalar(select(User).where(User.user_id == user))
    if customer not in await eligible_customers(session, target):
        raise Refused("no_active_binding_for_rfq_customer")
    scoped = await session.scalar(
        select(func.count()).where(
            RolePermission.role == target.role,
            RolePermission.permission.in_(RFQ_SCOPED_PERMISSIONS),
            RolePermission.allowed.is_(True),
        )
    )
    if not scoped:
        raise Refused("role_cannot_use_assignment")
    key = (RfqAssignment.rfq_id == rfq) & (RfqAssignment.user_id == user)
    existing = (await session.execute(select(RfqAssignment.revoked_at).where(key).with_for_update())).first()  # fmt: skip
    if existing is not None and existing.revoked_at is None:
        return Outcome("already_assigned", False)
    savepoint = await session.begin_nested()
    try:
        if existing is not None:
            await session.execute(update(RfqAssignment).where(key).values(revoked_at=None, assigned_by=operator))  # fmt: skip
            result = "reactivated"
        else:
            await session.execute(insert(RfqAssignment).values(rfq_id=rfq, user_id=user, assigned_by=operator))  # fmt: skip
            result = "assigned"
        await savepoint.commit()
    except IntegrityError:
        await savepoint.rollback()
        raise Refused("concurrent_change") from None  # another operator won the race
    await _audit(session, operator=operator, operation=OPERATIONS["grant"], outcome="success",
                 customer=customer, rfq=rfq, data={"target_user_id": user, "rfq_id": rfq, "reason": reason, "result": result})  # fmt: skip
    return Outcome(result, execute)


async def revoke_assignment(session, *, operator, user, rfq, reason, execute) -> Outcome:
    if not await _privileged(session, "rfq_assignments", "UPDATE") or not await _privileged(session, "audit_events", "INSERT"):  # fmt: skip
        raise Refused("database_role_lacks_privileges")
    await _authorize(session, operator, BINDING_PERMISSION, user)
    _identifier(user)
    customer = await _rfq_customer(session, rfq)
    changed = await session.execute(
        update(RfqAssignment)
        .where(
            RfqAssignment.rfq_id == rfq,
            RfqAssignment.user_id == user,
            RfqAssignment.revoked_at.is_(None),
        )  # fmt: skip
        .values(revoked_at=func.now())
    )
    if changed.rowcount == 0:
        return Outcome("already_revoked", False)
    await _audit(session, operator=operator, operation=OPERATIONS["revoke"], outcome="success",
                 customer=customer, rfq=rfq, data={"target_user_id": user, "rfq_id": rfq, "reason": reason})  # fmt: skip
    return Outcome("revoked", execute)


async def list_assignments(session, *, user=None, rfq=None) -> list[dict]:
    statement = select(RfqAssignment.rfq_id, RfqAssignment.user_id, RfqAssignment.revoked_at)
    if user is not None:
        _identifier(user)
        statement = statement.where(RfqAssignment.user_id == user)
    if rfq is not None:
        _identifier(rfq)
        statement = statement.where(RfqAssignment.rfq_id == rfq)
    rows = (await session.execute(statement.order_by(RfqAssignment.rfq_id, RfqAssignment.user_id))).all()  # fmt: skip
    return [
        {"rfq_id": r.rfq_id, "user_id": r.user_id, "active": r.revoked_at is None} for r in rows
    ]


async def verify_assignments(session, *, user=None) -> list[dict]:
    """Active assignments whose user is inactive or not actively bound to the RFQ's customer."""
    statement = (
        select(RfqAssignment.rfq_id, RfqAssignment.user_id, RFQ.customer_id, User.active)
        .join(RFQ, RFQ.rfq_id == RfqAssignment.rfq_id)
        .join(User, User.user_id == RfqAssignment.user_id)
        .where(RfqAssignment.revoked_at.is_(None))
    )
    if user is not None:
        _identifier(user)
        statement = statement.where(RfqAssignment.user_id == user)
    stale = []
    for row in (await session.execute(statement)).all():
        bound = await session.scalar(
            select(func.count()).where(
                UserCustomerBinding.user_id == row.user_id,
                UserCustomerBinding.customer_id == row.customer_id,
                UserCustomerBinding.revoked_at.is_(None),
            )
        )
        if not row.active or not bound:
            reason = "user_inactive" if not row.active else "no_active_binding"
            stale.append({"rfq_id": row.rfq_id, "user_id": row.user_id, "problem": reason})
    return stale


async def _run(args) -> dict:
    from rfq_copilot.config import DatabaseConfig
    from rfq_copilot.database import Database

    config = DatabaseConfig.from_env(args.target, env_file=Path(args.env_file))
    read_only = args.command in ("list", "verify")
    async with Database(config, read_only=read_only) as database:
        if read_only:
            async with database.session() as session:
                if args.command == "list":
                    return {"status": "ok", "assignments": await list_assignments(session, user=args.user, rfq=args.rfq)}  # fmt: skip
                stale = await verify_assignments(session, user=args.user)
                return {"status": "ok" if not stale else "attention", "stale": stale}
        try:
            async with database.session() as session:
                action = grant_assignment if args.command == "grant" else revoke_assignment
                outcome = await action(session, operator=args.operator, user=args.user, rfq=args.rfq,
                                       reason=args.reason, execute=args.execute)  # fmt: skip
                if outcome.executed:
                    await session.commit()
        except Refused as refused:
            if args.execute:
                await _audit_denial(database, args, refused.code)
            raise
    return {"status": "ok", "result": outcome.result, "executed": outcome.executed,
            "mode": "execute" if args.execute else "dry-run"}  # fmt: skip


async def _audit_denial(database, args, code: str) -> None:
    try:
        async with database.session() as session:
            known = await session.scalar(select(func.count()).where(User.user_id == args.operator))
            await _audit(session, operator=args.operator if known else None, operation=OPERATIONS[args.command],
                         outcome="denied", data={"target_user_id": args.user, "refusal": code})  # fmt: skip
            await session.commit()
    except Exception:  # noqa: BLE001 - auditing a refusal must not mask it
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=("development", "test"), default="development")
    parser.add_argument("--env-file")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("grant", "revoke"):
        command = sub.add_parser(name)
        command.add_argument("--user", required=True)
        command.add_argument("--rfq", required=True)
        command.add_argument("--operator", required=True)
        command.add_argument("--reason", choices=REASONS, required=True)
        command.add_argument("--execute", action="store_true")
    listing = sub.add_parser("list")
    scope = listing.add_mutually_exclusive_group(required=True)
    scope.add_argument("--user")
    scope.add_argument("--rfq")
    verify = sub.add_parser("verify")
    verify.add_argument("--user")
    args = parser.parse_args(argv)
    for name in ("execute", "rfq", "user"):
        if not hasattr(args, name):
            setattr(args, name, None if name != "execute" else False)
    try:
        result = asyncio.run(_run(args))
    except Refused as refused:
        print(json.dumps({"status": "refused", "error": refused.code}))
        return 2
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "error", "error": type(error).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
