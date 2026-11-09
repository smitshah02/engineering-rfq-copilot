"""Source, mapping, conversion and CLI boundary tests; frozen sources are read-only."""

import copy
import csv
import io
import json
import shutil
import zipfile
from dataclasses import replace
from datetime import UTC, date
from decimal import Decimal
from pathlib import Path

import pytest

from rfq_copilot.config import DatabaseConfig
from rfq_copilot.seeding import SeedError, cli, sources
from rfq_copilot.seeding.mapping import (
    EMPTY_TABLES,
    EXPECTED_COUNTS,
    TABLES,
    build_plan,
    dependency_order,
    validate_keys,
    validate_plan,
)
from rfq_copilot.seeding.safety import validate_target
from rfq_copilot.seeding.sources import (
    ALLOWED_SOURCES,
    load_sources,
    read_csv,
    safe_path,
    validate_version,
)
from rfq_copilot.seeding.transform import (
    calendar_date,
    convert,
    decimal_value,
    instant,
    integer,
    json_text,
    json_value,
)
from rfq_copilot.seeding.validation import validate_business

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/rfq_synthetic_data"
ARCHIVE = ROOT / "data/rfq_synthetic_data_v1.1.0.zip"


@pytest.fixture(scope="module")
def bundle():
    return load_sources(SOURCE, ARCHIVE)


@pytest.fixture
def plan(bundle, tmp_path):
    return build_plan(bundle, tmp_path / "snapshots")


def changed_csv(bundle, target, change):
    path = next(p for t, _, p, _ in sources.CSV_SOURCES if t == target)
    reader = csv.DictReader(io.StringIO(bundle.raw[path].decode("utf-8")))
    rows = list(reader)
    change(rows)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return replace(bundle, raw={**bundle.raw, path: output.getvalue().encode("utf-8")})


def test_source_inventory_and_checksums(bundle):
    assert bundle.integrity["dataset_files"] == 680  # the archive; OS metadata is not counted
    assert bundle.integrity["protected_hashes"] == 637
    assert len(bundle.raw) == len(ALLOWED_SOURCES) == 28
    assert all(
        not p.startswith(("evaluation/", "rfq_scenarios/", "attachments/")) for p in bundle.raw
    )


@pytest.fixture
def clean_extraction(tmp_path):
    """A fresh extraction of the frozen archive, as on a clean install (no .DS_Store)."""
    with zipfile.ZipFile(ARCHIVE) as package:
        package.extractall(tmp_path)
    try:
        yield tmp_path / "rfq_synthetic_data"
    finally:
        shutil.rmtree(tmp_path)  # never leave a stray copy of the hidden files behind


def test_clean_extraction_passes_without_os_metadata(clean_extraction):
    assert not any(p.name == ".DS_Store" for p in clean_extraction.rglob("*"))
    integrity = load_sources(clean_extraction, ARCHIVE).integrity
    assert integrity["dataset_files"] == 680
    assert integrity["protected_hashes"] == 637


def test_recognized_os_metadata_is_ignored_anywhere(clean_extraction):
    (clean_extraction / ".DS_Store").write_bytes(b"\0")
    (clean_extraction / "master_data/.DS_Store").write_bytes(b"\0")
    assert load_sources(clean_extraction, ARCHIVE).integrity["dataset_files"] == 680


@pytest.mark.parametrize(
    "relative",
    [
        "docs/unexpected.md",  # outside the protected roots
        "notes.txt",
        "master_data/extra.csv",  # inside a protected root
        "docs/.DS_Store.bak",  # only exact OS metadata names are recognized
        "docs/Thumbs.db",
        "src/__pycache__/generate_data.cpython-312.pyc",
    ],
)
def test_unexpected_dataset_file_fails_verification(clean_extraction, relative):
    path = clean_extraction / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unexpected")
    with pytest.raises(SeedError, match="differ"):
        load_sources(clean_extraction, ARCHIVE)


@pytest.mark.parametrize("relative", ["docs/business_rules.md", "master_data/customers.csv"])
def test_missing_dataset_file_fails_verification(clean_extraction, relative):
    (clean_extraction / relative).unlink()
    with pytest.raises(SeedError, match="differ"):
        load_sources(clean_extraction, ARCHIVE)


def test_changed_protected_file_still_fails_its_checksum(clean_extraction):
    path = clean_extraction / "master_data/customers.csv"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(SeedError, match="protected checksum mismatch"):
        load_sources(clean_extraction, ARCHIVE)


def test_hidden_files_are_never_json_reader_inputs(monkeypatch):
    seen = []
    original = sources.json_value

    def traced(value, location):
        seen.append(location)
        return original(value, location)

    monkeypatch.setattr(sources, "json_value", traced)
    bundle = load_sources(SOURCE, ARCHIVE)
    # Hash verification streams hidden bytes; only the hash manifest is parsed.
    assert seen == ["manifest"]
    seen.clear()
    build_plan(bundle, ROOT / "data/rfq_seed_snapshots")
    assert set(seen) <= ALLOWED_SOURCES
    assert not any(s.startswith(("evaluation/", "rfq_scenarios/")) for s in seen)


@pytest.mark.parametrize(
    "path",
    [
        "evaluation/ground_truth/frozen_evaluation/a.json",
        "evaluation/security_scenarios.json",
        "rfq_scenarios/development/a.json",
        "rfq_scenarios/validation/a.json",
        "rfq_scenarios/frozen_evaluation/a.json",
        "attachments/attachment_manifest.json",
    ],
)
def test_nonoperational_json_reader_refused(bundle, path):
    with pytest.raises(SeedError, match="not allowlisted"):
        bundle.json(path)


@pytest.mark.parametrize("path", ["../.env", "/etc/passwd", "master_data/../x", "a\\b"])
def test_path_escape_rejected(path):
    with pytest.raises(SeedError):
        safe_path(SOURCE, path)


def test_symlink_source_rejected(tmp_path):
    (tmp_path / "linked.csv").symlink_to(SOURCE / "master_data/customers.csv")
    with pytest.raises(SeedError, match="symlink"):
        safe_path(tmp_path, "linked.csv")


def test_missing_source_fails_without_disclosing_path(tmp_path):
    with pytest.raises(SeedError, match="missing"):
        load_sources(tmp_path, ARCHIVE)


def test_archive_checksum_mismatch(tmp_path):
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"invalid archive")
    with pytest.raises(SeedError, match="checksum mismatch"):
        load_sources(SOURCE, archive)


@pytest.mark.parametrize("tamper", ["protected", "config", "manifest", "extra", "missing"])
def test_tampered_copies_are_refused(tmp_path, tamper):
    # The original dataset is never changed. No copied labels are parsed.
    copied = tmp_path / "dataset"
    shutil.copytree(SOURCE, copied)
    if tamper == "protected":
        (copied / "master_data/customers.csv").write_bytes(b"changed")
    elif tamper == "config":
        (copied / "config/pricing_rules.json").write_bytes(b"{}")
    elif tamper == "manifest":
        (copied / sources.MANIFEST).write_bytes(b"{}")
    elif tamper == "extra":
        (copied / "master_data/extra.csv").write_bytes(b"x")
    else:
        (copied / "master_data/customers.csv").unlink()
    with pytest.raises(SeedError):
        load_sources(copied, ARCHIVE)


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", "1.0.0"),
        ("version", None),
        ("reference_date", "2026-11-03"),
        ("planning_weeks", 9),
        ("schedule_start", "2026-11-10"),
    ],
)
def test_dataset_version_and_provenance_rejected(bundle, key, value):
    with pytest.raises(SeedError):
        validate_version({**bundle.generation, key: value})


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b'a,b\n"unterminated,1',
        b"a,a\n1,2\n",
        b"a,c\n1,2\n",
        b"a,b\n1\n",
        b"a,b\n1,2,3\n",
        b"a,b\n\xff,2\n",
    ],
)
def test_malformed_or_uncontracted_csv(raw):
    with pytest.raises(SeedError):
        read_csv(raw, ["a", "b"], "test CSV")


def test_csv_header_order_is_reliable():
    assert read_csv(b"b,a\n2,1\n", ["a", "b"], "CSV") == [{"a": "1", "b": "2"}]


@pytest.mark.parametrize(
    "raw", ['{"a":1,"a":2}', '{"a":}', '{"n":NaN}', '{"n":Infinity}', "\ufeff{}"]
)
def test_malformed_json_and_duplicate_keys(raw):
    with pytest.raises(SeedError):
        json_value(raw, "test JSON")


@pytest.mark.parametrize("value", ["1.001", "NaN", "Infinity", "10000000000000000", "", 0.1, True])
def test_invalid_currency_is_rejected(value):
    with pytest.raises(SeedError):
        decimal_value(value, "money", 18, 2)


def test_exact_decimal_and_nested_json_precision():
    assert decimal_value("1234567890123456.78", "money", 18, 2) == Decimal("1234567890123456.78")
    assert decimal_value("1.23000", "money", 18, 2) == Decimal("1.23")
    value = json_value('{"number":0.123456789012345678901,"evidence":"0.584307360"}', "JSON")
    assert value["number"] == Decimal("0.123456789012345678901")
    assert json_value(json_text(value), "JSON") == value


@pytest.mark.parametrize("value", ["1.0", "01", " 1", True, 1.5, "2147483648"])
def test_strict_integer(value):
    with pytest.raises(SeedError):
        integer(value, "integer")


@pytest.mark.parametrize("value", ["20261102", "2026-02-30", "2026-1-02", "2026-11-02T00:00:00Z"])
def test_bad_calendar_date(value):
    with pytest.raises(SeedError):
        calendar_date(value, "date")


@pytest.mark.parametrize("value", ["2026-11-02", "2026-11-02T00:00:00", "invalid"])
def test_naive_or_bad_timestamp(value):
    with pytest.raises(SeedError):
        instant(value, "timestamp")


def test_dates_timezones_booleans_and_null_handling():
    assert calendar_date("2026-11-02", "date") == date(2026, 11, 2)
    assert instant("2026-11-02T07:00:00-05:00", "timestamp") == instant(
        "2026-11-02T12:00:00Z", "timestamp"
    )
    assert instant("2026-11-02T12:00:00Z", "timestamp").tzinfo == UTC
    assert convert("False", TABLES["users"].c.active, "bool") is False
    assert convert("", TABLES["quotes"].c.submitted_by, "optional") is None
    assert convert("none", TABLES["finishes"].c.finish_id, "finish") == "none"
    with pytest.raises(SeedError):
        convert("true", TABLES["users"].c.active, "bool")
    with pytest.raises(SeedError):
        convert("", TABLES["users"].c.user_id, "required")


def test_all_33_table_classifications_and_dependency_order(plan):
    assert {n: len(r) for n, r in plan.rows.items()} == EXPECTED_COUNTS
    assert sum(map(len, plan.rows.values())) == 1089
    assert len(EMPTY_TABLES) == 7
    positions = {n: i for i, n in enumerate(dependency_order())}
    for name, table in TABLES.items():
        for fk in table.foreign_key_constraints:
            if not fk.deferrable:
                assert positions[fk.referred_table.name] < positions[name]


def test_historical_projection_provenance(plan):
    versions = plan.rows["quote_versions"]
    assert sum(r["snapshot_completeness"] == "metadata_only" for r in versions) == 16
    assert sum(r["snapshot_completeness"] == "priced_partial" for r in versions) == 80
    for r in versions:
        assert r["manufacturing_rule_version"] is None and r["calculation_id"] is None
        if not r["is_current"]:
            assert r["total_usd"] is None and r["pricing_rule_version"] is None
    assert all(r["approval_request_id"] is None for r in plan.rows["approval_history"])
    assert all(r["status"] == "archived" and r["created_by"] is None for r in plan.rows["rfqs"])
    assert (
        sum(r["expected_replenishment_date"] is None for r in plan.rows["material_inventory"]) == 75
    )


def test_version_field_discrepancy_is_resolved_without_default(plan):
    rule = next(
        r for r in plan.rows["business_rule_versions"] if r["rule_kind"] == "business_constraints"
    )
    assert rule["rule_version"] == "business_constraints@sha256:" + rule["sha256"]
    assert "version" not in rule["document"] and "properties" not in rule["document"]
    handoff = next(r for r in plan.rows["business_rule_versions"] if r["rule_kind"] == "handoff")
    assert handoff["document"]["properties"]["schema_version"]["const"] == "1.1"


@pytest.mark.parametrize(
    "target", ["customers", "role_permissions", "quote_versions", "material_thickness_options"]
)
def test_duplicate_and_composite_keys_fail(plan, target):
    plan.rows[target].append(copy.deepcopy(plan.rows[target][0]))
    with pytest.raises(SeedError, match="duplicate"):
        validate_keys(plan)


@pytest.mark.parametrize(
    "target,column",
    [
        ("contacts", "customer_id"),
        ("operation_routing", "operation_id"),
        ("material_inventory", "thickness_mm"),
        ("approval_history", "quote_version"),
        ("quotes", "current_version"),
    ],
)
def test_foreign_key_tuples_fail(plan, target, column):
    plan.rows[target][0][column] = (
        999 if column in {"thickness_mm", "quote_version", "current_version"} else "missing"
    )
    with pytest.raises(SeedError, match="foreign-key"):
        validate_keys(plan)


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("customers", "credit_limit_usd", "0.001"),
        ("users", "active", "yes"),
        ("quotes", "created_at", "2026-11-02T12:00:00"),
        ("material_inventory", "available_kg", "999"),
        ("production_capacity", "utilization_fraction", "0.1111"),
        ("quote_line_items", "part_json", "{}"),
    ],
)
def test_invalid_source_record_fails(bundle, tmp_path, target, field, value):
    changed = changed_csv(bundle, target, lambda r: r[0].update({field: value}))
    with pytest.raises(SeedError):
        build_plan(changed, tmp_path / "snapshots")


@pytest.mark.parametrize(
    "violation",
    ["holes", "hidden_part", "hidden_cost", "quantity", "total", "current", "approval", "ordinals"],
)
def test_nested_and_cross_record_validation(plan, violation):
    line = plan.rows["quote_line_items"][0]
    if violation == "holes":
        line["part_json"]["hole_count"] = 301
    elif violation == "hidden_part":
        line["part_json"]["expected_outcome"] = "sentinel-hidden-answer"
    elif violation == "hidden_cost":
        line["cost_breakdown_json"]["expected_pricing"] = "sentinel-hidden-answer"
    elif violation == "quantity":
        line["part_json"]["quantity"] += 1
    elif violation == "total":
        line["line_total_usd"] += Decimal("0.01")
    elif violation == "current":
        plan.rows["quote_versions"][0]["is_current"] = not plan.rows["quote_versions"][0][
            "is_current"
        ]
    elif violation == "approval":
        plan.rows["approval_history"][0]["actor_id"] = "USER-001"
    else:
        line["line_number"] = 99
    with pytest.raises(SeedError) as error:
        validate_business(plan)
    assert "sentinel-hidden-answer" not in str(error.value)


def test_snapshot_cannot_write_inside_frozen_dataset(bundle):
    with pytest.raises(SeedError):
        build_plan(bundle, SOURCE / "snapshots")


@pytest.mark.parametrize("value", [None, 0.1, "1.00"])
def test_native_records_cannot_bypass_decimal_or_null_validation(plan, value):
    plan.rows["customers"][0]["credit_limit_usd"] = value
    with pytest.raises(SeedError):
        validate_plan(plan)


def test_cli_success_summary_and_exit_code(monkeypatch, tmp_path, capsys):
    async def successful(*args, **kwargs):
        return {"status": "PASS", "inserted": 0, "mode": "dry-run"}

    monkeypatch.setattr(cli, "run_seed", successful)
    arguments = [
        "--target",
        "development",
        "--source",
        str(SOURCE),
        "--archive",
        str(ARCHIVE),
        "--snapshot-dir",
        str(tmp_path / "snapshots"),
        "--env-file",
        str(ROOT / ".env"),
        "--dry-run",
    ]
    assert cli.main(arguments) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "PASS", "inserted": 0, "mode": "dry-run"}
    assert captured.err == ""


@pytest.mark.parametrize(
    "database,host",
    [
        ("rfq_copilot_test", "localhost"),
        ("production", "localhost"),
        ("rfq_copilot_dev", "remote.invalid"),
    ],
)
def test_explicit_development_target_cannot_use_wrong_database(database, host):
    from sqlalchemy.engine import make_url

    config = DatabaseConfig(make_url(f"postgresql+asyncpg://tester:private@{host}/{database}"))
    with pytest.raises(SeedError) as error:
        validate_target(config)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "arguments",
    [[], ["--target", "production"], ["--target", "development", "--apply", "--dry-run"]],
)
def test_cli_ambiguous_or_missing_arguments_fail(arguments):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2


def test_cli_error_is_json_and_never_echoes_bad_environment(tmp_path, capsys):
    env = tmp_path / "env"
    env.write_text("DATABASE_URL=private-credential-sentinel\n")
    code = cli.main(
        [
            "--target",
            "development",
            "--source",
            str(SOURCE),
            "--archive",
            str(ARCHIVE),
            "--snapshot-dir",
            str(tmp_path / "snapshots"),
            "--env-file",
            str(env),
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1 and json.loads(captured.err)["status"] == "FAIL"
    assert "private-credential-sentinel" not in captured.err + captured.out
