"""Versioned, SHA-256-pinned permission-policy overlays on top of the frozen baseline.

Dataset v1.1.0 (6 roles, 12 permissions, 27 grants) is never edited. An approved policy
change ships as a version-controlled file under ``rfq_copilot/policies/`` whose SHA-256
is pinned in ``TRUSTED_POLICIES``. It is applied only after the baseline seed, by an
explicit operator command, and only when the active schema contract
(``schema_contract.active().policy_overlays``) accepts it:

    python -m rfq_copilot.seeding.policy --target development --env-file .env \\
        --confirm-database rfq_copilot_dev rbac-policy-v2

The seeding verifier and the integrity audit then accept exactly baseline + the
complete overlay: every overlay row present, unchanged, and nothing else. A partial or
modified overlay, or an overlay row under a contract that does not accept it, fails.

``rbac-policy-v2`` adds the ``identity_operator`` role (schema revision 20261111_06),
the dedicated route permissions of policy v2 and the two operator permissions, granted
only to ``identity_operator``. It assigns no user to any role.
"""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from sqlalchemy import func, insert, select

from rfq_copilot.models import Permission, Role, RolePermission
from rfq_copilot.schema_contract import active
from rfq_copilot.seeding import SeedError

POLICIES_DIR = Path(__file__).resolve().parents[1] / "policies"
# version -> (file name, SHA-256 of the exact file bytes)
TRUSTED_POLICIES = {
    "rbac-policy-v2": (
        "rbac-policy-v2.json",
        "3e2ac34cfbe47d4cf4ff0dcdc58fb0c95d29cfce97673d3b7c366addbfa1b74d",
    ),
}
OVERLAY_TABLES = ("roles", "permissions", "role_permissions")
_KEYS = {"version", "builds_on", "roles", "permissions", "grants"}


def policy_rows(version: str) -> dict[str, tuple[dict, ...]]:
    """The exact rows a trusted overlay adds, after checking its file against the pin."""
    if version not in TRUSTED_POLICIES:
        raise SeedError(f"{version} is not an approved permission policy.")
    name, pinned = TRUSTED_POLICIES[version]
    raw = (POLICIES_DIR / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != pinned:
        raise SeedError(f"{name} does not match its pinned SHA-256.")
    document = json.loads(raw)
    roles, permissions, grants = document["roles"], document["permissions"], document["grants"]
    valid = (
        set(document) == _KEYS
        and document["version"] == version
        and all(isinstance(x, str) and x for x in [*roles, *permissions])
        and len(set(roles)) == len(roles)
        and len(set(permissions)) == len(permissions)
        and all(isinstance(g, list) and len(g) == 2 for g in grants)
        and len({tuple(g) for g in grants}) == len(grants)
    )
    if not valid:
        raise SeedError(f"{name} is not a well-formed permission policy.")
    return {
        "roles": tuple({"role": r} for r in roles),
        "permissions": tuple({"permission": p} for p in permissions),
        "role_permissions": tuple({"role": r, "permission": p, "allowed": True} for r, p in grants),
    }


def _accepted():
    return {version: policy_rows(version) for version in sorted(active().policy_overlays)}


def is_policy_extra(table: str, record: dict) -> bool:
    """True only for a stored row that equals a row of an overlay the contract accepts."""
    if table not in OVERLAY_TABLES:
        return False
    return any(dict(record) in rows[table] for rows in _accepted().values())


def overlay_state(extras: dict[str, list[dict]]) -> str | None:
    """'absent', 'applied:<version>', or None for a partial or mixed overlay."""
    found = {t: {tuple(sorted(r.items())) for r in extras.get(t, ())} for t in OVERLAY_TABLES}
    if not any(found.values()):
        return "absent"
    for version, rows in _accepted().items():
        wanted = {t: {tuple(sorted(r.items())) for r in rows[t]} for t in OVERLAY_TABLES}
        if found == wanted:
            return f"applied:{version}"
    return None


async def register_policy(session, version: str) -> str:
    """Insert the complete overlay ('applied') or confirm it ('already present').

    Caller-owned session; never commits. Refuses an overlay the active contract does not
    accept, an unseeded baseline, and a partial or conflicting existing overlay.
    """
    if version not in active().policy_overlays:
        raise SeedError(f"{version} is not accepted by the active schema contract.")
    rows = policy_rows(version)
    overlay_roles = {r["role"] for r in rows["roles"]}
    needed = {g["role"] for g in rows["role_permissions"]} - overlay_roles
    seeded = await session.scalar(select(func.count()).select_from(RolePermission))
    present_roles = set((await session.scalars(select(Role.role))).all())
    if not seeded or not needed <= present_roles:
        raise SeedError("Seed the baseline first: the roles this policy grants to are missing.")
    stored = {
        "roles": [{"role": r} for r in present_roles if r in overlay_roles],
        "permissions": [
            {"permission": p}
            for p in (await session.scalars(select(Permission.permission))).all()
            if {"permission": p} in rows["permissions"]
        ],
        "role_permissions": [
            {"role": r, "permission": p, "allowed": a}
            for r, p, a in (
                await session.execute(
                    select(RolePermission.role, RolePermission.permission, RolePermission.allowed)
                )
            ).all()
            if (r, p) in {(g["role"], g["permission"]) for g in rows["role_permissions"]}
        ],
    }
    state = overlay_state(stored)
    if state == f"applied:{version}":
        return "already present"
    if state != "absent":
        raise SeedError("A partial or modified policy overlay is stored; repair it manually.")
    await session.execute(insert(Role), list(rows["roles"]))
    await session.execute(insert(Permission), list(rows["permissions"]))
    await session.execute(insert(RolePermission), list(rows["role_permissions"]))
    return "applied"


def main(argv=None) -> int:
    from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
    from rfq_copilot.database import Database, DatabaseError
    from rfq_copilot.seeding.safety import validate_target, verify_live_identity, verify_schema

    parser = argparse.ArgumentParser(description="Apply an approved permission-policy overlay.")
    parser.add_argument("--target", choices=("development", "test"), required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--confirm-database", required=True)
    parser.add_argument("policy_version")
    args = parser.parse_args(argv)

    async def run() -> str:
        config = DatabaseConfig.from_env(args.target, env_file=Path(args.env_file))
        validate_target(config)
        if args.confirm_database != config.url.database:
            raise SeedError("Confirmation does not match the target database.")
        async with Database(config) as database:
            async with database.session() as session:
                connection = await session.connection()
                await connection.run_sync(verify_live_identity, config)
                await connection.run_sync(verify_schema, config)
                status = await register_policy(session, args.policy_version)
                await session.commit()
                return status

    try:
        status = asyncio.run(run())
    except (SeedError, DatabaseConfigurationError, DatabaseError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "PASS", "policy_version": args.policy_version, "result": status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
