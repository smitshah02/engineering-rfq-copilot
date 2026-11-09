"""Allowlisted readers and streaming frozen-source integrity verification."""

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.transform import json_value, require

DATASET_VERSION = "1.1.0"
ARCHIVE_SHA256 = "681a882fd7c22cd4e874b57fd31ab6bd12602bc826b2c02cfa79eb17a5ad5080"
MANIFEST = "evaluation/checksums.sha256.json"
ARCHIVE_ROOT = "rfq_synthetic_data/"
DATASET_FILES = 680  # files in the frozen archive
# Exact file names only; anything else not in the archive fails verification.
OS_METADATA = frozenset({".DS_Store"})
PROTECTED_ROOTS = (
    "master_data",
    "operations_data",
    "transactional_data",
    "rfq_scenarios",
    "attachments",
    "evaluation",
    "contracts",
)
# Table, formal source entity, relative file, required v1.1.0 row count.
CSV_SOURCES = (
    ("customers", "customer", "master_data/customers.csv", 40),
    ("contacts", "contact", "master_data/contacts.csv", 40),
    ("materials", "material", "master_data/materials.csv", 15),
    ("finishes", "finish", "master_data/finishes.csv", 7),
    ("operations", "operation", "master_data/operations.csv", 8),
    ("workcentres", "workcentre", "master_data/workcentres.csv", 8),
    ("suppliers", "supplier", "master_data/suppliers.csv", 6),
    ("users", "user", "master_data/users.csv", 24),
    ("role_permissions", None, "master_data/roles_permissions.csv", 27),
    ("material_inventory", "inventory", "operations_data/material_inventory.csv", 90),
    ("production_capacity", "capacity", "operations_data/production_capacity.csv", 80),
    ("operation_routing", "routing", "operations_data/operation_routing.csv", 28),
    ("procurement_records", "procurement", "operations_data/procurement_records.csv", 12),
    ("quotes", "quote", "transactional_data/historical_quotes.csv", 80),
    ("quote_versions", "quote_version", "transactional_data/quote_versions.csv", 96),
    ("quote_line_items", "quote_line", "transactional_data/quote_line_items.csv", 100),
    ("approval_history", "approval", "transactional_data/approval_history.csv", 70),
)
RULE_SOURCES = (
    ("manufacturing", "config/manufacturing_rules.json", "mfg-v1.0"),
    ("pricing", "config/pricing_rules.json", "price-v1.0"),
    ("approval", "config/approval_policies.json", "approval-v1.1"),
    ("workflow", "config/workflow_state_rules.json", "workflow-v1.0"),
    ("entity_contract", "contracts/entity_schemas.json", None),
    ("tool_contract", "contracts/tool_schemas.json", None),
    ("business_constraints", "contracts/business_constraints.json", None),
    ("coordination", "contracts/agent_coordination_policy.json", None),
    ("handoff", "contracts/agent_handoff_schema.json", None),
)
RFQS = "transactional_data/historical_rfqs.json"
GENERATION = "config/generation_config.yaml"
ALLOWED_SOURCES = frozenset(
    [p for _, _, p, _ in CSV_SOURCES] + [p for _, p, _ in RULE_SOURCES] + [RFQS, GENERATION]
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    require(
        isinstance(relative, str)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in relative
        and path.as_posix() == relative,
        "source",
        "unsafe relative path",
    )
    result = root.joinpath(*path.parts)
    require(
        all(not p.is_symlink() for p in [result, *result.parents] if p != root.parent),
        "source",
        "symlinks are forbidden",
    )
    require(result.resolve().is_relative_to(root.resolve()), "source", "path escapes dataset")
    require(result.is_file(), "source", "required file is missing")
    return result


def read_csv(raw: bytes, columns: list[str], location: str) -> list[dict]:
    try:
        reader = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
        header = next(reader)
        require(len(header) == len(set(header)), location, "duplicate CSV column")
        require(set(header) == set(columns), location, "CSV columns differ from contract")
        result = []
        for number, values in enumerate(reader, 2):
            require(len(values) == len(header), f"{location}:{number}", "malformed CSV row")
            result.append(dict(zip(header, values, strict=True)))
        return result
    except (csv.Error, UnicodeError, StopIteration):
        raise SeedError(f"{location}: malformed UTF-8 CSV.") from None


@dataclass(frozen=True)
class SourceBundle:
    raw: dict[str, bytes] = field(repr=False)
    generation: dict = field(repr=False)
    integrity: dict
    source_directory: Path

    def json(self, relative: str):
        require(
            relative in ALLOWED_SOURCES and relative.endswith(".json"), "source", "not allowlisted"
        )
        return json_value(self.raw[relative], relative)


def load_sources(source: Path, archive: Path) -> SourceBundle:
    """Read hidden assets only as bounded binary blocks for hashing, never as JSON."""
    try:
        require(source.is_dir() and not source.is_symlink(), "source", "dataset directory missing")
        require(archive.is_file() and not archive.is_symlink(), "archive", "archive missing")
        require(sha256_file(archive) == ARCHIVE_SHA256, "archive", "checksum mismatch")
        with zipfile.ZipFile(archive) as package:
            manifest_raw = safe_path(source, MANIFEST).read_bytes()
            archived_manifest = package.read("rfq_synthetic_data/" + MANIFEST)
            require(manifest_raw == archived_manifest, "manifest", "differs from frozen archive")
            manifest = json_value(manifest_raw, "manifest")
            require(
                isinstance(manifest, dict) and len(manifest) == 637,
                "manifest",
                "unexpected shape/count",
            )
            discovered = {
                p.relative_to(source).as_posix()
                for name in PROTECTED_ROOTS
                for p in (source / name).rglob("*")
                if p.is_file()
                and p.name not in OS_METADATA
                and p.relative_to(source).as_posix() != MANIFEST
            }
            require(discovered == set(manifest), "manifest", "protected file set differs")
            # Strict inventory: the extracted files must be exactly the archive's files,
            # except for recognized OS metadata that a desktop adds after extraction.
            archived = {
                name.removeprefix(ARCHIVE_ROOT)
                for name in package.namelist()
                if name.startswith(ARCHIVE_ROOT) and not name.endswith("/")
            }
            extracted = {
                p.relative_to(source).as_posix()
                for p in source.rglob("*")
                if p.is_file() and p.name not in OS_METADATA
            }
            require(len(archived) == DATASET_FILES, "archive", "unexpected file count")
            require(extracted == archived, "source", "dataset files differ from frozen archive")
            for relative, digest in manifest.items():
                require(
                    isinstance(digest, str) and bool(re.fullmatch("[0-9a-f]{64}", digest)),
                    "manifest",
                    "invalid digest",
                )
                require(
                    sha256_file(safe_path(source, relative)) == digest,
                    "manifest",
                    "protected checksum mismatch",
                )
            raw = {}
            for relative in sorted(ALLOWED_SOURCES):
                content = safe_path(source, relative).read_bytes()
                require(
                    content == package.read("rfq_synthetic_data/" + relative),
                    relative,
                    "differs from frozen archive",
                )
                raw[relative] = content
        # This small provenance document has no monetary values. SafeLoader never executes tags.
        generation = yaml.safe_load(raw[GENERATION].decode("utf-8"))
        validate_version(generation)
        return SourceBundle(
            raw,
            generation,
            {
                "dataset_version": DATASET_VERSION,
                "dataset_files": len(extracted),
                "protected_hashes": len(manifest),
                "archive_sha256": ARCHIVE_SHA256,
                "mapped_files": len(ALLOWED_SOURCES),
                "business_csv_files": len(CSV_SOURCES),
                "excluded": ["evaluation/**", "rfq_scenarios/**", "attachments/**"],
            },
            source.resolve(),
        )
    except (OSError, UnicodeError, KeyError, zipfile.BadZipFile, yaml.YAMLError):
        raise SeedError("Dataset/archive could not be read or is malformed.") from None


def validate_version(generation) -> None:
    require(isinstance(generation, dict), GENERATION, "expected object")
    require(generation.get("version") == DATASET_VERSION, GENERATION, "unsupported dataset version")
    require(
        generation.get("reference_date") == "2026-11-02", GENERATION, "unexpected reference date"
    )
    require(
        generation.get("schedule_start") == "2026-11-09", GENERATION, "unexpected schedule start"
    )
    require(
        generation.get("planning_weeks") == 10 and generation.get("currency") == "USD",
        GENERATION,
        "unexpected baseline conventions",
    )
