"""Explicit-target seeding CLI. Its only output is a credential-safe JSON summary."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
from rfq_copilot.database import DatabaseError
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.loader import run_seed
from rfq_copilot.seeding.mapping import build_plan
from rfq_copilot.seeding.sources import load_sources


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed the frozen synthetic baseline atomically.")
    parser.add_argument("--target", choices=["development", "test"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--confirm-database", help="For apply: repeat the exact approved database name"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", dest="mode", action="store_const", const="dry-run")
    action.add_argument("--apply", dest="mode", action="store_const", const="apply")
    action.add_argument("--verify", dest="mode", action="store_const", const="verify")
    args = parser.parse_args(argv)
    try:
        config = DatabaseConfig.from_env(args.target, env_file=Path(args.env_file))
        if args.snapshot_dir.resolve().is_relative_to(args.source.resolve()):
            raise SeedError("Snapshot directory must be outside the frozen source tree.")
        bundle = load_sources(args.source, args.archive)
        plan = build_plan(bundle, args.snapshot_dir)
        result = asyncio.run(
            run_seed(config, plan, args.mode, confirm_database=args.confirm_database)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (SeedError, DatabaseConfigurationError, DatabaseError) as error:
        print(
            json.dumps({"status": "FAIL", "error": str(error), "transaction": "not_committed"}),
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error": "Invalid source, configuration or snapshot artifact.",
                    "transaction": "not_committed",
                }
            ),
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(
            json.dumps({"status": "CANCELLED", "transaction": "verify_outcome_before_retry"}),
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
