"""Operator-only identity administration: customer bindings and token revocation.

    python -m rfq_copilot.identity_admin inspect --user U --env-file .env
    python -m rfq_copilot.identity_admin grant  --user U --customer C --operator O --reason onboarding [--execute]
    python -m rfq_copilot.identity_admin revoke --user U --customer C --operator O --reason offboarding [--execute]
    python -m rfq_copilot.identity_admin revoke-token --user U --operator O --reason security (--token-stdin | --all-before-now) [--execute]

There is no HTTP route for any of this. Every mutating command:
- is a dry run unless ``--execute`` is given (the transaction is rolled back);
- needs the connected database role to hold the table privileges it writes with
  (``has_table_privilege``; no superuser, no CREATEROLE), so the operator runs it with a
  maintenance role, not the API's read role;
- needs ``--operator`` to be an active ``business.users`` row whose role is granted the
  operator permission in ``business.role_permissions`` (``manage_customer_bindings`` or
  ``revoke_tokens``). Only the pinned ``rbac-policy-v2`` overlay grants them, and only to
  the dedicated ``identity_operator`` role (revision 20261111_06); administrators and
  agents never hold them. Until the overlay is applied and a separately provisioned
  operator account holds that role, every execution is refused;
- refuses self-administration (operator == target user);
- validates the user (active) and customer (exists, status not ``inactive``), refuses a
  conflicting active binding, treats a repeat as a no-op;
- writes the change and an ``audit_events`` row in one transaction; a refusal during
  ``--execute`` is audited in its own transaction (outcome ``denied``);
- prints fixed result codes as JSON; never the token, credentials or customer text.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.models import AuditEvent, Customer, RolePermission, User, UserCustomerBinding
from rfq_copilot.repositories.identity import ELIGIBLE_ACCOUNT_STATUSES
from rfq_copilot.repositories.queries import _identifier

BINDING_PERMISSION = "manage_customer_bindings"
REVOCATION_PERMISSION = "revoke_tokens"
REASONS = ("onboarding", "offboarding", "correction", "security")
# Audit operation names (audit_events.operation is free text; these are proposed names).
OPERATIONS = {
    "grant": "customer_binding.grant",
    "revoke": "customer_binding.revoke",
    "revoke-token": "token.revoke",
}


class Refused(Exception):
    """A fixed, non-sensitive refusal code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Outcome:
    result: str  # granted | reactivated | already_bound | revoked | already_revoked | token_revoked | user_tokens_revoked
    executed: bool


async def _privileged(session: AsyncSession, table: str, privileges: str) -> bool:
    return bool(
        await session.scalar(
            text("SELECT has_table_privilege(current_user, :t, :p)"),
            {"t": f"business.{table}", "p": privileges},
        )
    )


async def _authorize(session: AsyncSession, operator: str, permission: str, target: str) -> None:
    _identifier(operator)
    if operator == target:
        raise Refused("self_administration_refused")
    holder = await session.scalar(
        select(func.count())
        .select_from(User)
        .join(RolePermission, RolePermission.role == User.role)
        .where(
            User.user_id == operator,
            User.active.is_(True),
            RolePermission.permission == permission,
            RolePermission.allowed.is_(True),
        )
    )
    if not holder:
        raise Refused("operator_not_authorized")


async def _active_user(session: AsyncSession, user_id: str) -> None:
    _identifier(user_id)
    if not await session.scalar(select(func.count()).where(User.user_id == user_id, User.active.is_(True))):  # fmt: skip
        raise Refused("user_not_active")


async def _audit(
    session, *, operator, operation, outcome, customer=None, data=None, rfq=None
) -> None:
    await session.execute(
        insert(AuditEvent).values(
            event_id=uuid.uuid4(),
            actor_id=operator,
            customer_id=customer,
            rfq_id=rfq,
            operation=operation,
            outcome=outcome,
            correlation_id=f"identity-admin-{uuid.uuid4()}",
            event_data=data or {},
        )
    )


async def grant_binding(session, *, operator, user, customer, reason, execute) -> Outcome:
    if not await _privileged(session, "user_customer_bindings", "INSERT,UPDATE") or not await _privileged(session, "audit_events", "INSERT"):  # fmt: skip
        raise Refused("database_role_lacks_privileges")
    await _authorize(session, operator, BINDING_PERMISSION, user)
    await _active_user(session, user)
    _identifier(customer)
    status = await session.scalar(select(Customer.account_status).where(Customer.customer_id == customer))  # fmt: skip
    if status is None or status not in ELIGIBLE_ACCOUNT_STATUSES:
        raise Refused("customer_not_eligible")
    rows = (
        await session.execute(
            select(UserCustomerBinding.customer_id, UserCustomerBinding.revoked_at)
            .where(UserCustomerBinding.user_id == user)
            .with_for_update()  # serializes concurrent grants for this user
        )
    ).all()
    same = next((row for row in rows if row.customer_id == customer), None)
    if same is not None and same.revoked_at is None:
        return Outcome("already_bound", False)
    from rfq_copilot.schema_contract import active

    if not active().multi_customer_bindings and any(row.revoked_at is None for row in rows):
        raise Refused("conflicting_active_binding")  # one customer per user: revoke first
    if same is not None:
        await session.execute(
            update(UserCustomerBinding)
            .where(UserCustomerBinding.user_id == user, UserCustomerBinding.customer_id == customer)
            .values(revoked_at=None)
        )
        result = "reactivated"
    else:
        await session.execute(insert(UserCustomerBinding).values(user_id=user, customer_id=customer))  # fmt: skip
        result = "granted"
    await _audit(session, operator=operator, operation=OPERATIONS["grant"], outcome="success",
                 customer=customer, data={"target_user_id": user, "reason": reason, "result": result})  # fmt: skip
    return Outcome(result, execute)


async def revoke_binding(session, *, operator, user, customer, reason, execute) -> Outcome:
    if not await _privileged(session, "user_customer_bindings", "UPDATE") or not await _privileged(session, "audit_events", "INSERT"):  # fmt: skip
        raise Refused("database_role_lacks_privileges")
    await _authorize(session, operator, BINDING_PERMISSION, user)
    _identifier(user)
    _identifier(customer)
    changed = await session.execute(
        update(UserCustomerBinding)
        .where(
            UserCustomerBinding.user_id == user,
            UserCustomerBinding.customer_id == customer,
            UserCustomerBinding.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
    )
    if changed.rowcount == 0:
        return Outcome("already_revoked", False)
    await _audit(session, operator=operator, operation=OPERATIONS["revoke"], outcome="success",
                 customer=customer, data={"target_user_id": user, "reason": reason})  # fmt: skip
    return Outcome("revoked", execute)


async def revoke_tokens(session, *, operator, user, reason, token, execute) -> Outcome:
    """Revoke one token (``token`` given) or every token of ``user`` issued before now."""
    from rfq_copilot.models import TokenRevocation

    token_revocations = TokenRevocation.__table__

    if not await _privileged(session, "token_revocations", "INSERT") or not await _privileged(session, "audit_events", "INSERT"):  # fmt: skip
        raise Refused("database_role_lacks_privileges")
    await _authorize(session, operator, REVOCATION_PERMISSION, user)
    _identifier(user)
    if not await session.scalar(select(func.count()).where(User.user_id == user)):
        raise Refused("user_unknown")
    values = {"revocation_id": uuid.uuid4(), "user_id": user, "created_by": operator, "reason": reason}  # fmt: skip
    if token is not None:
        digest = hashlib.sha256(token.encode()).hexdigest()  # the raw token is never stored
        exists = await session.scalar(
            select(func.count())
            .select_from(token_revocations)
            .where(token_revocations.c.token_sha256 == digest)  # fmt: skip
        )
        if exists:
            return Outcome("token_revoked", False)
        values["token_sha256"] = digest
        result = "token_revoked"
    else:
        values["not_before"] = func.clock_timestamp()  # the moment of revocation, not txn start
        result = "user_tokens_revoked"
    await session.execute(insert(token_revocations).values(**values))
    await _audit(session, operator=operator, operation=OPERATIONS["revoke-token"], outcome="success",
                 data={"target_user_id": user, "reason": reason, "scope": "token" if token else "user"})  # fmt: skip
    return Outcome(result, execute)


async def inspect_user(session, *, user) -> dict:
    _identifier(user)
    active = await session.scalar(select(User.active).where(User.user_id == user))
    if active is None:
        raise Refused("user_unknown")
    rows = (
        await session.execute(
            select(UserCustomerBinding.customer_id, UserCustomerBinding.revoked_at)
            .where(UserCustomerBinding.user_id == user)
            .order_by(UserCustomerBinding.customer_id)
        )
    ).all()
    return {
        "user_active": bool(active),
        "bindings": [{"customer_id": r.customer_id, "active": r.revoked_at is None} for r in rows],
    }


async def _run(args) -> dict:
    from rfq_copilot.config import DatabaseConfig
    from rfq_copilot.database import Database

    config = DatabaseConfig.from_env(args.target, env_file=Path(args.env_file))
    read_only = args.command == "inspect"
    async with Database(config, read_only=read_only) as database:
        if read_only:
            async with database.session() as session:
                return {"status": "ok", **await inspect_user(session, user=args.user)}
        try:
            async with database.session() as session:
                outcome = await _dispatch(session, args)
                if outcome.executed:
                    await session.commit()
                # Otherwise the session context rolls the dry run back.
        except Refused as refused:
            if args.execute:
                await _audit_denial(database, args, refused.code)
            raise
    return {"status": "ok", "result": outcome.result, "executed": outcome.executed,
            "mode": "execute" if args.execute else "dry-run"}  # fmt: skip


async def _dispatch(session, args) -> Outcome:
    common = {"operator": args.operator, "user": args.user, "reason": args.reason, "execute": args.execute}  # fmt: skip
    if args.command == "grant":
        return await grant_binding(session, customer=args.customer, **common)
    if args.command == "revoke":
        return await revoke_binding(session, customer=args.customer, **common)
    token = sys.stdin.readline().strip() if args.token_stdin else None
    if args.token_stdin and not token:
        raise Refused("token_missing")
    return await revoke_tokens(session, token=token, **common)


async def _audit_denial(database, args, code: str) -> None:
    """Record a refused execution in its own transaction; never fails the refusal."""
    try:
        async with database.session() as session:
            known = await session.scalar(select(func.count()).where(User.user_id == args.operator))
            await _audit(session, operator=args.operator if known else None,
                         operation=OPERATIONS[args.command], outcome="denied",
                         data={"target_user_id": args.user, "refusal": code})  # fmt: skip
            await session.commit()
    except Exception:  # noqa: BLE001 - auditing a refusal must not mask it
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=("development", "test"), default="development")
    parser.add_argument("--env-file")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--user", required=True)
    for name in ("grant", "revoke", "revoke-token"):
        command = sub.add_parser(name)
        command.add_argument("--user", required=True)
        command.add_argument("--operator", required=True)
        command.add_argument("--reason", choices=REASONS, required=True)
        command.add_argument("--execute", action="store_true")
        if name != "revoke-token":
            command.add_argument("--customer", required=True)
        else:
            scope = command.add_mutually_exclusive_group(required=True)
            scope.add_argument("--token-stdin", action="store_true")
            scope.add_argument("--all-before-now", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        args.execute = False
    try:
        result = asyncio.run(_run(args))
    except Refused as refused:
        print(json.dumps({"status": "refused", "error": refused.code}))
        return 2
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "error", "error": type(error).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
