"""Registration of approved rule versions that are not part of the frozen v1.1.0 baseline.

Such a rule ships as a version-controlled file under rfq_copilot/rules/ and must match its
trusted-manifest entry (source sha256 and canonical fingerprint) before it is written. The
seeding --verify and integrity audits accept exactly these extra rows and nothing else.

    python -m rfq_copilot.seeding.rules --target development --env-file .env \
        --confirm-database rfq_copilot_dev approval-v1.2
"""

import argparse
import asyncio
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import insert, select

from rfq_copilot.canonical import fingerprint
from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
from rfq_copilot.database import Database, DatabaseError
from rfq_copilot.models import BusinessRuleVersion
from rfq_copilot.repositories import get_rule_version
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.safety import validate_target, verify_live_identity, verify_schema

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"
# Approved post-baseline rule versions and the baseline rule each one requires.
REGISTERED = {"approval-v1.2": ("approval-v1.2.json", "approval-v1.1")}


def registered_rule_row(rule_version: str) -> dict:
    """The row to store for a registered rule, after checking its file against the manifest."""
    if rule_version not in REGISTERED or rule_version not in TRUSTED_RULES:
        raise SeedError(f"{rule_version} is not an approved post-baseline rule version.")
    path = RULES_DIR / REGISTERED[rule_version][0]
    raw = path.read_bytes()
    document = json.loads(raw, parse_float=Decimal)
    kind, sha256, expected = TRUSTED_RULES[rule_version]
    if (hashlib.sha256(raw).hexdigest(), fingerprint(document), document.get("version")) != (
        sha256,
        expected,
        rule_version,
    ):
        raise SeedError(f"{path.name} does not match its trusted-manifest entry.")
    return {
        "rule_version": rule_version,
        "rule_kind": kind,
        "sha256": sha256,
        "document": document,
        "source_path": f"src/rfq_copilot/rules/{path.name}",
    }


def is_registered_extra(table: str, record: dict, baseline_versions) -> bool:
    """True only for a stored rule row outside the baseline that matches the manifest exactly."""
    if table != "business_rule_versions" or record["rule_version"] in baseline_versions:
        return False
    trusted = TRUSTED_RULES.get(record["rule_version"])
    if record["rule_version"] not in REGISTERED or trusted is None:
        return False
    return trusted == (record["rule_kind"], record["sha256"], fingerprint(record["document"]))


async def register_rule_version(session, rule_version: str) -> str:
    """Insert the rule if absent ('registered'), or confirm the stored copy ('already present').

    Caller-owned session; never commits. The baseline rule it builds on must be seeded.
    """
    row = registered_rule_row(rule_version)
    base = REGISTERED[rule_version][1]
    if await get_rule_version(session, row["rule_kind"], base) is None:
        raise SeedError(f"Seed the baseline first: {base} is not stored.")
    exists = await session.scalar(
        select(BusinessRuleVersion.rule_version).where(
            BusinessRuleVersion.rule_version == rule_version
        )
    )
    if exists is None:
        await session.execute(insert(BusinessRuleVersion).values(**row))
    # Read back through the verifying repository: manifest, kind and version checks.
    await get_rule_version(session, row["rule_kind"], rule_version, expected_sha256=row["sha256"])
    return "already present" if exists else "registered"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Register an approved post-baseline rule.")
    parser.add_argument("rule_version", choices=sorted(REGISTERED))
    parser.add_argument("--target", choices=["development", "test"], required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--confirm-database", required=True, help="Repeat the database name")
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
                status = await register_rule_version(session, args.rule_version)
                await session.commit()
                return status

    try:
        status = asyncio.run(run())
    except (SeedError, DatabaseConfigurationError, DatabaseError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "PASS", "rule_version": args.rule_version, "result": status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
