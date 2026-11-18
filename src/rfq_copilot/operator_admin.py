"""Operator-only provisioning of human ``identity_operator`` and external ``customer`` accounts.

    python -m rfq_copilot.operator_admin provision-customer --new-user U --display-name N \\
        --email E --region R --subject S --verification-ref SEC-123 \\
        --approved-by ADMIN --requested-by OPERATOR [--execute]
    (then bind it: python -m rfq_copilot.identity_admin grant ...; same rules below, no
    bootstrap; needs the rbac-policy-v3 overlay)

    python -m rfq_copilot.operator_admin provision-service --new-user U --display-name N \\
        --email E --region R --subject svc|rfq-workflow-C --verification-ref CHG-123 \\
        --customer C --approved-by ADMIN --requested-by OPERATOR [--execute]
    (Checkpoint A2: an internal ``rfq_workflow_agent`` service identity bound to exactly one
    customer in the same transaction; needs the rbac-policy-v4 overlay; machine subject only)

    python -m rfq_copilot.operator_admin provision --new-user U --display-name N \\
        --email E --region R --subject S --verification-ref SEC-123 \\
        --approved-by ADMIN [--requested-by OPERATOR | --bootstrap] [--execute]
    python -m rfq_copilot.operator_admin deactivate --operator-user U --approved-by ADMIN \\
        --requested-by OPERATOR --reason offboarding [--execute]

Rules (fixed refusal codes, nothing sensitive printed):
- Dry run unless ``--execute``; one transaction; an ``audit_events`` row is written with
  the change. A refused execution is audited separately (outcome ``denied``).
- The connected database role must hold the table privileges it writes with.
- The pinned ``rbac-policy-v2`` overlay must be applied (the role row exists and holds
  exactly the two operator permissions); operators get nothing else.
- **Explicit administrator authorization:** ``--approved-by`` must be an active
  ``administrator``. The administrator gains nothing; the approval is recorded.
- **Two people:** once any active operator exists, ``--requested-by`` must be a
  different active operator holding ``manage_customer_bindings``. ``--bootstrap`` (no
  requester) is accepted only while no active operator exists.
- **Verified human identity:** ``--verification-ref`` (ticket form ``ABC-123``) records
  the out-of-band identity check. Subjects of machine identities (prefixes ``agent|``,
  ``svc|``, ``service|``, ``bot|``, ``m2m|``, ``client|``, or the ``@clients`` suffix)
  are refused, and the account is always a new, separate user: an existing user is never
  converted, so administrators are never given operator authority.
- Conflicts (user id, authentication subject or email already used) are refused.
"""

import argparse
import asyncio
import json
import re
from pathlib import Path

from sqlalchemy import func, insert, select, update

from rfq_copilot.identity_admin import (
    REASONS,
    Outcome,
    Refused,
    _active_user,
    _audit,
    _privileged,
)
from rfq_copilot.models import RolePermission, User
from rfq_copilot.repositories.queries import _identifier

OPERATOR_ROLE = "identity_operator"
APPROVER_ROLE = "administrator"
OPERATOR_PERMISSIONS = frozenset({"manage_customer_bindings", "revoke_tokens"})
VERIFICATION_REF = re.compile(r"^[A-Z]{2,10}-[0-9]{1,10}$")
MACHINE_PREFIXES = ("agent|", "svc|", "service|", "bot|", "m2m|", "client|")
OPERATIONS = {
    "provision": "operator.provision",
    "deactivate": "operator.deactivate",
    "provision-customer": "customer.provision",
    "provision-service": "service.provision",
}
# Checkpoint A2 (rbac-policy-v4): the internal workflow service role. Exactly these grants;
# never approve/reject/release/request_approval/edit or any customer_* permission.
SERVICE_ROLE = "rfq_workflow_agent"
SERVICE_PERMISSIONS = frozenset(
    {"view_catalog", "view_assigned_rfqs", "view_quote", "create_quote_draft"}
)
SERVICE_PREFIX = "svc|"
CUSTOMER_ROLE = "customer"
CUSTOMER_PERMISSIONS = frozenset(
    {"view_customer", "customer_view_rfq", "customer_create_rfq", "customer_view_quote",
     "customer_attach_rfq_document"}
)  # fmt: skip


async def _approver(session, approver: str) -> None:
    _identifier(approver)
    found = await session.scalar(
        select(func.count()).where(
            User.user_id == approver, User.active.is_(True), User.role == APPROVER_ROLE
        )
    )
    if not found:
        raise Refused("approval_requires_active_administrator")


async def _role_ready(session, role: str, expected: frozenset[str], code: str) -> None:
    granted = set(
        (
            await session.scalars(
                select(RolePermission.permission).where(
                    RolePermission.role == role, RolePermission.allowed.is_(True)
                )
            )
        ).all()  # fmt: skip
    )
    if granted != expected:
        raise Refused(code)  # overlay missing or modified


async def _operator_role_ready(session) -> None:
    await _role_ready(session, OPERATOR_ROLE, OPERATOR_PERMISSIONS, "operator_role_not_provisioned")


async def _active_operators(session) -> int:
    return await session.scalar(
        select(func.count()).where(User.role == OPERATOR_ROLE, User.active.is_(True))
    )


async def _requester(session, requester: str | None, approver: str, bootstrap: bool) -> None:
    existing = await _active_operators(session)
    if existing == 0:
        if not bootstrap or requester is not None:
            raise Refused("bootstrap_required_for_first_operator")
        return
    if bootstrap:
        raise Refused("bootstrap_only_without_operators")
    if requester is None:
        raise Refused("requester_required")
    _identifier(requester)
    if requester == approver:
        raise Refused("two_person_rule")
    holds = await session.scalar(
        select(func.count())
        .select_from(User)
        .join(RolePermission, RolePermission.role == User.role)
        .where(
            User.user_id == requester,
            User.active.is_(True),
            User.role == OPERATOR_ROLE,
            RolePermission.permission == "manage_customer_bindings",
            RolePermission.allowed.is_(True),
        )
    )
    if not holds:
        raise Refused("requester_not_an_operator")


def _human_subject(subject: str) -> None:
    lowered = subject.lower()
    if not subject or subject != subject.strip() or len(subject) > 255 or not subject.isprintable():
        raise Refused("invalid_subject")
    if lowered.startswith(MACHINE_PREFIXES) or lowered.endswith("@clients"):
        raise Refused("machine_identity_refused")


async def _new_account_checks(session, new_user, display_name, email, region, subject, verification_ref) -> None:  # fmt: skip
    if not await _privileged(session, "users", "INSERT") or not await _privileged(session, "audit_events", "INSERT"):  # fmt: skip
        raise Refused("database_role_lacks_privileges")
    _identifier(new_user)
    if not VERIFICATION_REF.match(verification_ref or ""):
        raise Refused("verification_reference_required")
    _human_subject(subject)
    if not display_name.strip() or "@" not in email or not region.strip():
        raise Refused("invalid_profile")


async def _no_conflict(session, new_user, subject, email) -> None:
    conflict = await session.scalar(
        select(func.count()).where(
            (User.user_id == new_user)
            | (User.authentication_subject == subject)
            | (func.lower(User.email) == email.lower())
        )
    )
    if conflict:
        raise Refused("identity_conflict")  # never converts or reuses an existing account


async def provision_customer(
    session, *, new_user, display_name, email, region, subject, verification_ref,
    approver, requester, execute,
) -> Outcome:  # fmt: skip
    """A new external ``customer`` account: requested by an operator, approved by an
    administrator, never bound here (bind with ``identity_admin grant``, audited)."""
    await _new_account_checks(session, new_user, display_name, email, region, subject, verification_ref)  # fmt: skip
    await _role_ready(session, CUSTOMER_ROLE, CUSTOMER_PERMISSIONS, "customer_role_not_provisioned")
    await _approver(session, approver)
    if not await _active_operators(session):
        raise Refused("operator_required")  # no bootstrap path for customer accounts
    await _requester(session, requester, approver, bootstrap=False)
    if new_user in (approver, requester):
        raise Refused("self_administration_refused")
    await _no_conflict(session, new_user, subject, email)
    await session.execute(
        insert(User).values(
            user_id=new_user, display_name=display_name, role=CUSTOMER_ROLE, email=email,
            active=True, assigned_region=region, authentication_subject=subject,
        )
    )  # fmt: skip
    await _audit(session, operator=requester, operation=OPERATIONS["provision-customer"],
                 outcome="success",
                 data={"new_user_id": new_user, "approved_by": approver, "requested_by": requester,
                       "verification_ref": verification_ref})  # fmt: skip
    return Outcome("customer_provisioned", execute)


def _service_subject(subject: str) -> None:
    """A machine subject under the dedicated ``svc|`` namespace, never a human one."""
    if not subject or subject != subject.strip() or len(subject) > 255 or not subject.isprintable():
        raise Refused("invalid_subject")
    if not subject.startswith(SERVICE_PREFIX) or len(subject) == len(SERVICE_PREFIX):
        raise Refused("service_subject_required")


async def provision_service(
    session, *, new_user, display_name, email, region, subject, verification_ref, customer,
    approver, requester, execute,
) -> Outcome:  # fmt: skip
    """A new internal ``rfq_workflow_agent`` account, requested by an operator, approved by
    an administrator and bound to exactly one customer through the audited binding path, in
    one transaction. An existing account is never converted."""
    from rfq_copilot.identity_admin import grant_binding

    if not await _privileged(session, "users", "INSERT") or not await _privileged(session, "audit_events", "INSERT"):  # fmt: skip
        raise Refused("database_role_lacks_privileges")
    _identifier(new_user)
    _identifier(customer)
    if not VERIFICATION_REF.match(verification_ref or ""):
        raise Refused("verification_reference_required")
    _service_subject(subject)
    if not display_name.strip() or "@" not in email or not region.strip():
        raise Refused("invalid_profile")
    await _role_ready(session, SERVICE_ROLE, SERVICE_PERMISSIONS, "service_role_not_provisioned")
    await _approver(session, approver)
    if not await _active_operators(session):
        raise Refused("operator_required")
    await _requester(session, requester, approver, bootstrap=False)
    if new_user in (approver, requester):
        raise Refused("self_administration_refused")
    await _no_conflict(session, new_user, subject, email)
    await session.execute(
        insert(User).values(
            user_id=new_user, display_name=display_name, role=SERVICE_ROLE, email=email,
            active=True, assigned_region=region, authentication_subject=subject,
        )
    )  # fmt: skip
    await _audit(session, operator=requester, operation=OPERATIONS["provision-service"],
                 outcome="success",
                 data={"new_user_id": new_user, "approved_by": approver, "requested_by": requester,
                       "verification_ref": verification_ref, "customer_id": customer})  # fmt: skip
    bound = await grant_binding(session, operator=requester, user=new_user, customer=customer,
                                reason="onboarding", execute=execute)  # fmt: skip
    if bound.result != "granted":
        raise Refused("service_binding_failed")
    return Outcome("service_provisioned", execute)


async def provision_operator(
    session, *, new_user, display_name, email, region, subject, verification_ref,
    approver, requester, bootstrap, execute,
) -> Outcome:  # fmt: skip
    await _new_account_checks(session, new_user, display_name, email, region, subject, verification_ref)  # fmt: skip
    await _operator_role_ready(session)
    await _approver(session, approver)
    await _requester(session, requester, approver, bootstrap)
    if new_user in (approver, requester):
        raise Refused("self_administration_refused")
    await _no_conflict(session, new_user, subject, email)
    await session.execute(
        insert(User).values(
            user_id=new_user, display_name=display_name, role=OPERATOR_ROLE, email=email,
            active=True, assigned_region=region, authentication_subject=subject,
        )
    )  # fmt: skip
    await _audit(session, operator=requester or approver, operation=OPERATIONS["provision"],
                 outcome="success",
                 data={"new_user_id": new_user, "approved_by": approver, "requested_by": requester,
                       "verification_ref": verification_ref, "bootstrap": bootstrap})  # fmt: skip
    return Outcome("operator_provisioned", execute)


async def deactivate_operator(session, *, operator_user, approver, requester, reason, execute) -> Outcome:  # fmt: skip
    if not await _privileged(session, "users", "UPDATE") or not await _privileged(session, "audit_events", "INSERT"):  # fmt: skip
        raise Refused("database_role_lacks_privileges")
    _identifier(operator_user)
    await _approver(session, approver)
    if requester is not None and requester == operator_user:
        raise Refused("self_administration_refused")
    role = await session.scalar(select(User.role).where(User.user_id == operator_user))
    if role != OPERATOR_ROLE:
        raise Refused("not_an_operator")  # this tool never touches other accounts
    changed = await session.execute(
        update(User).where(User.user_id == operator_user, User.active.is_(True)).values(active=False)
    )  # fmt: skip
    if changed.rowcount == 0:
        return Outcome("already_inactive", False)
    await _audit(session, operator=approver, operation=OPERATIONS["deactivate"], outcome="success",
                 data={"target_user_id": operator_user, "requested_by": requester, "reason": reason})  # fmt: skip
    return Outcome("operator_deactivated", execute)


async def _run(args) -> dict:
    from rfq_copilot.config import DatabaseConfig
    from rfq_copilot.database import Database

    async with Database(
        DatabaseConfig.from_env(args.target, env_file=Path(args.env_file))
    ) as database:
        try:
            async with database.session() as session:
                if args.command == "provision":
                    outcome = await provision_operator(
                        session, new_user=args.new_user, display_name=args.display_name,
                        email=args.email, region=args.region, subject=args.subject,
                        verification_ref=args.verification_ref, approver=args.approved_by,
                        requester=args.requested_by, bootstrap=args.bootstrap, execute=args.execute,
                    )  # fmt: skip
                elif args.command == "provision-service":
                    outcome = await provision_service(
                        session, new_user=args.new_user, display_name=args.display_name,
                        email=args.email, region=args.region, subject=args.subject,
                        verification_ref=args.verification_ref, customer=args.customer,
                        approver=args.approved_by, requester=args.requested_by, execute=args.execute,
                    )  # fmt: skip
                elif args.command == "provision-customer":
                    outcome = await provision_customer(
                        session, new_user=args.new_user, display_name=args.display_name,
                        email=args.email, region=args.region, subject=args.subject,
                        verification_ref=args.verification_ref, approver=args.approved_by,
                        requester=args.requested_by, execute=args.execute,
                    )  # fmt: skip
                else:
                    outcome = await deactivate_operator(
                        session, operator_user=args.operator_user, approver=args.approved_by,
                        requester=args.requested_by, reason=args.reason, execute=args.execute,
                    )  # fmt: skip
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
            known = await session.scalar(select(func.count()).where(User.user_id == args.approved_by))  # fmt: skip
            await _audit(session, operator=args.approved_by if known else None,
                         operation=OPERATIONS[args.command], outcome="denied", data={"refusal": code})  # fmt: skip
            await session.commit()
    except Exception:  # noqa: BLE001 - auditing a refusal must not mask it
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=("development", "test"), default="development")
    parser.add_argument("--env-file")
    sub = parser.add_subparsers(dest="command", required=True)
    provision = sub.add_parser("provision", help="create a new human identity_operator account")
    for flag in ("--new-user", "--display-name", "--email", "--region", "--subject", "--verification-ref", "--approved-by"):  # fmt: skip
        provision.add_argument(flag, required=True)
    who = provision.add_mutually_exclusive_group(required=True)
    who.add_argument("--requested-by")
    who.add_argument("--bootstrap", action="store_true", help="first operator only")
    provision.add_argument("--execute", action="store_true")
    customer = sub.add_parser("provision-customer", help="create a new external customer account")
    for flag in ("--new-user", "--display-name", "--email", "--region", "--subject", "--verification-ref", "--approved-by", "--requested-by"):  # fmt: skip
        customer.add_argument(flag, required=True)
    customer.add_argument("--execute", action="store_true")
    service = sub.add_parser(
        "provision-service", help="create an internal workflow service identity"
    )
    for flag in ("--new-user", "--display-name", "--email", "--region", "--subject", "--verification-ref", "--customer", "--approved-by", "--requested-by"):  # fmt: skip
        service.add_argument(flag, required=True)
    service.add_argument("--execute", action="store_true")
    deactivate = sub.add_parser("deactivate", help="deactivate an identity_operator account")
    deactivate.add_argument("--operator-user", required=True)
    deactivate.add_argument("--approved-by", required=True)
    deactivate.add_argument("--requested-by")
    deactivate.add_argument("--reason", choices=REASONS, required=True)
    deactivate.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "provision" and args.requested_by is None:
        args.requested_by = None
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


__all__ = ["deactivate_operator", "provision_operator", "provision_service", "_active_user"]
