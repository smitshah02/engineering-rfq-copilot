"""Independent expectations come from approved Day 4 documents, never model-generated fixtures."""

import copy
import html
import importlib
import json
import re
import subprocess
import sys
import warnings
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import CheckConstraint, Numeric, UniqueConstraint, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import MANYTOONE, configure_mappers, joinedload, selectinload
from sqlalchemy.schema import AddConstraint, CreateIndex, CreateTable

from rfq_copilot.models import (
    MODEL_CLASSES,
    RFQ,
    ApprovalHistory,
    AuditEvent,
    Base,
    Contact,
    Customer,
    Material,
    MaterialInventory,
    MaterialThicknessOption,
    Quote,
    QuoteLineItem,
    QuoteVersion,
)

ROOT = Path(__file__).resolve().parents[1]
APPROVED_TABLES = (
    "business_rule_versions",
    "data_snapshots",
    "roles",
    "permissions",
    "role_permissions",
    "users",
    "customers",
    "contacts",
    "materials",
    "finishes",
    "operations",
    "workcentres",
    "suppliers",
    "material_thickness_options",
    "material_finish_compatibility",
    "material_supported_operations",
    "finish_material_categories",
    "operation_routing",
    "material_inventory",
    "procurement_records",
    "production_capacity",
    "rfqs",
    "rfq_line_items",
    "rfq_attachments",
    "pricing_calculations",
    "quotes",
    "quote_versions",
    "quote_line_items",
    "approval_requests",
    "approval_history",
    "idempotency_records",
    "audit_events",
    "workflow_runs",
)
# Approved after Day 4: TD-012, revision 20261110_01. Specified here, as TD-005 is below;
# the Day 4 documents stay as written. It deliberately has no ORM relationships.
TD012_TABLE = "user_customer_bindings"
TD012_SPEC = dict(
    columns=[
        dict(name="user_id", type="TEXT", nullable=False, default="none", rule="—"),
        dict(name="customer_id", type="TEXT", nullable=False, default="none", rule="—"),
        dict(
            name="created_at",
            type="TIMESTAMPTZ",
            nullable=False,
            default="CURRENT_TIMESTAMP",
            rule="—",
        ),  # fmt: skip
        dict(name="revoked_at", type="TIMESTAMPTZ", nullable=True, default="none", rule="—"),
    ],
    pk=("user_id", "customer_id"),
    unique=[],
    fks=[
        dict(local=("user_id",), target="users", remote=("user_id",), deferred=False),
        dict(local=("customer_id",), target="customers", remote=("customer_id",), deferred=False),
    ],
    checks=["revoked_at IS NULL OR revoked_at >= created_at"],
)
# Approved Phase 3.5 tables (revisions 20261111_02/03), specified here like TD-012; no
# ORM relationships. Identity and hex checks come from the shared Day 4 conventions.
PHASE35_SPECS = {
    "rfq_assignments": dict(
        columns=[
            dict(name="rfq_id", type="TEXT", nullable=False, default="none", rule="—"),
            dict(name="user_id", type="TEXT", nullable=False, default="none", rule="—"),
            dict(name="assigned_by", type="TEXT", nullable=False, default="none", rule="—"),
            dict(
                name="created_at",
                type="TIMESTAMPTZ",
                nullable=False,
                default="CURRENT_TIMESTAMP",
                rule="—",
            ),  # fmt: skip
            dict(name="revoked_at", type="TIMESTAMPTZ", nullable=True, default="none", rule="—"),
        ],
        pk=("rfq_id", "user_id"),
        unique=[],
        fks=[
            dict(local=("rfq_id",), target="rfqs", remote=("rfq_id",), deferred=False),
            dict(local=("user_id",), target="users", remote=("user_id",), deferred=False),
            dict(local=("assigned_by",), target="users", remote=("user_id",), deferred=False),
        ],
        checks=["revoked_at IS NULL OR revoked_at >= created_at"],
    ),
    "token_revocations": dict(
        columns=[
            dict(
                name="revocation_id",
                type="UUID",
                nullable=False,
                default="application UUIDv4",
                rule="—",
            ),  # fmt: skip
            dict(name="user_id", type="TEXT", nullable=False, default="none", rule="—"),
            dict(
                name="token_sha256",
                type="TEXT",
                nullable=True,
                default="none",
                rule="64 lowercase hexadecimal characters",
            ),  # fmt: skip
            dict(name="not_before", type="TIMESTAMPTZ", nullable=True, default="none", rule="—"),
            dict(name="expires_at", type="TIMESTAMPTZ", nullable=True, default="none", rule="—"),
            dict(name="created_by", type="TEXT", nullable=False, default="none", rule="—"),
            dict(name="reason", type="TEXT", nullable=False, default="none", rule="—"),
            dict(
                name="created_at",
                type="TIMESTAMPTZ",
                nullable=False,
                default="CURRENT_TIMESTAMP",
                rule="—",
            ),  # fmt: skip
        ],
        pk=("revocation_id",),
        unique=[],
        fks=[
            dict(local=("user_id",), target="users", remote=("user_id",), deferred=False),
            dict(local=("created_by",), target="users", remote=("user_id",), deferred=False),
        ],
        checks=[
            "(token_sha256 IS NULL) <> (not_before IS NULL)",
            "length(btrim(reason)) > 0 AND length(reason) <= 200",
        ],
    ),
}
SCHEMA_TABLES = (*APPROVED_TABLES, TD012_TABLE, *PHASE35_SPECS)
PG = postgresql.dialect()


def normalized(sql):
    """Formatting-only normalization: retain case and operators/literal values."""
    return re.sub(r"\s+", "", str(sql))


@pytest.fixture(scope="module")
def approved():
    """Parse complete column/key/check expectations from approved Markdown, independently."""
    text = (ROOT / "docs/database/table_schemas.md").read_text()
    sections = re.split(r"^## \d+\. `(\w+)`\s*$", text, flags=re.M)
    tables = {}
    for position in range(1, len(sections), 2):
        name, body = sections[position : position + 2]
        columns = []
        for line in body.splitlines():
            if not re.match(r"^\| `\w+` \|", line):
                continue
            values = [html.unescape(value.strip()) for value in line.split("|")[1:-1]]
            column, datatype, nullable, default, _, rule = values
            columns.append(
                dict(
                    name=column.strip("`"),
                    type=datatype.strip("`"),
                    nullable=nullable == "Yes",
                    default=default.strip("`"),
                    rule=rule,
                )
            )
        pk = re.search(r"\*\*Primary key:\*\* `\(([^)]+)\)`", body)[1].split(", ")
        unique_line = re.search(r"\*\*Unique constraints:\*\* (.*)", body)[1]
        unique = [tuple(value.split(", ")) for value in re.findall(r"`\(([^)]+)\)`", unique_line)]
        fks = []
        for left, target, right, suffix in re.findall(
            r"^- `\(([^)]+)\) → (\w+)\(([^)]+)\)`(.*)$", body, re.M
        ):
            fks.append(
                dict(
                    local=tuple(left.split(", ")),
                    target=target,
                    remote=tuple(right.split(", ")),
                    deferred="DEFERRABLE" in suffix,
                )
            )
        check_line = re.search(r"\*\*Additional CHECKs:\*\* (.*)", body)[1]
        tables[name] = dict(
            columns=columns,
            pk=tuple(pk),
            unique=unique,
            fks=fks,
            checks=re.findall(r"`([^`]+)`", check_line),
        )
    assert set(tables) == set(APPROVED_TABLES)
    # Approved after Day 4: TD-005, revision 20261109_01. The Day 4 document stays as
    # written; this is the only delta, and the original key is asserted before replacing it.
    assert tables["production_capacity"]["unique"] == [("workcentre_id", "week_start")]
    tables["production_capacity"]["unique"] = [("workcentre_id", "week_start", "snapshot_id")]
    assert sum(len(table["columns"]) for table in tables.values()) == 261
    assert sum(len(table["fks"]) for table in tables.values()) == 64
    tables[TD012_TABLE] = copy.deepcopy(TD012_SPEC)
    # Approved Phase 3.5 deltas (20261111_04, 20261111_06): asserted as documented first.
    roles_check = "role IN ('sales_representative','estimator','manufacturing_engineer','sales_manager','administrator','auditor')"  # fmt: skip
    assert tables["roles"]["checks"] == [roles_check]
    tables["roles"]["checks"] = [roles_check[:-1] + ",'identity_operator')"]
    operation = next(c for c in tables["idempotency_records"]["checks"] if c.startswith("operation IN"))  # fmt: skip
    assert operation.endswith("'edit_quote_draft')")
    tables["idempotency_records"]["checks"] = [
        operation[:-1] + ",'attach_rfq_document')" if c == operation else c
        for c in tables["idempotency_records"]["checks"]
    ]
    for name, spec in PHASE35_SPECS.items():
        tables[name] = copy.deepcopy(spec)
    return tables


@pytest.fixture(scope="module", autouse=True)
def configured_models():
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        configure_mappers()
        list(Base.metadata.sorted_tables)


def orm_table(name):
    return Base.metadata.tables[
        ("orchestration" if name == "workflow_runs" else "business") + "." + name
    ]


def test_exact_registry_and_single_base():
    assert len(MODEL_CLASSES) == len(Base.registry.mappers) == 36
    assert {table.name for table in Base.metadata.tables.values()} == set(SCHEMA_TABLES)
    assert {mapper.class_ for mapper in Base.registry.mappers} == set(MODEL_CLASSES)
    assert all(
        model.metadata is Base.metadata and model.registry is Base.registry
        for model in MODEL_CLASSES
    )
    assert {table.schema for table in Base.metadata.tables.values()} == {
        "business",
        "orchestration",
    }
    assert orm_table("workflow_runs").schema == "orchestration"
    assert all(
        orm_table(name).schema == "business" for name in SCHEMA_TABLES if name != "workflow_runs"
    )


@pytest.mark.parametrize("name", SCHEMA_TABLES)
def test_all_columns_types_nullability_and_primary_keys_match_day4(approved, name):
    expectation = approved[name]
    table = orm_table(name)
    assert tuple(table.c.keys()) == tuple(col["name"] for col in expectation["columns"])
    assert tuple(table.primary_key.columns.keys()) == expectation["pk"]
    for expected in expectation["columns"]:
        actual = table.c[expected["name"]]
        assert actual.nullable is expected["nullable"]
        compiled = normalized(actual.type.compile(dialect=PG))
        wanted = expected["type"].replace("TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE")
        assert compiled == normalized(wanted)
        if expected["name"] in expectation["pk"]:
            assert not actual.nullable
        if expected["type"].startswith("NUMERIC"):
            assert isinstance(actual.type, Numeric) and actual.type.asdecimal is True
            assert actual.type.python_type is Decimal
        if expected["type"] == "TIMESTAMPTZ":
            assert actual.type.timezone is True
        if expected["type"] == "JSONB":
            assert isinstance(actual.type, JSONB) and actual.type.none_as_null is True
        if expected["type"] == "UUID":
            assert actual.type.as_uuid is True


@pytest.mark.parametrize("name", SCHEMA_TABLES)
def test_foreign_and_unique_keys_match_day4(approved, name):
    table = orm_table(name)
    expected = approved[name]
    found = {
        (
            tuple(fk.column_keys),
            next(iter(fk.elements)).column.table.name,
            tuple(element.column.name for element in fk.elements),
            bool(fk.deferrable),
        )
        for fk in table.foreign_key_constraints
    }
    wanted = {(fk["local"], fk["target"], fk["remote"], fk["deferred"]) for fk in expected["fks"]}
    assert found == wanted
    assert len(table.foreign_key_constraints) == len(wanted)
    for fk in table.foreign_key_constraints:
        assert fk.ondelete == fk.onupdate == "RESTRICT"
        assert fk.match == "SIMPLE"
        assert fk.initially == ("DEFERRED" if fk.deferrable else None)
        target = next(iter(fk.elements)).column.table
        targets = tuple(element.column.name for element in fk.elements)
        assert targets in [tuple(target.primary_key.columns.keys())] + [
            tuple(unique.columns.keys())
            for unique in target.constraints
            if isinstance(unique, UniqueConstraint)
        ]
    uniques = {
        tuple(key.columns.keys()) for key in table.constraints if isinstance(key, UniqueConstraint)
    }
    assert uniques == set(expected["unique"])
    assert len([key for key in table.constraints if isinstance(key, UniqueConstraint)]) == len(
        uniques
    )


@pytest.mark.parametrize("name", SCHEMA_TABLES)
def test_insert_defaults_and_computed_values_match_day4(approved, name):
    for expected in approved[name]["columns"]:
        column = orm_table(name).c[expected["name"]]
        default = expected["default"]
        assert (
            column.onupdate is None
            and column.server_onupdate is None
            or column.computed is not None
        )
        if default.startswith("generated: "):
            assert column.computed is not None and column.computed.persisted is True
            assert normalized(column.computed.sqltext) == normalized(
                default.removeprefix("generated: ")
            )
            assert column.default is None
        elif default == "application UUIDv4":
            assert column.default.is_callable and column.server_default is None
            first, second = column.default.arg(None), column.default.arg(None)
            assert isinstance(first, UUID) and first.version == 4 and first != second
        elif default == "none":
            assert (
                column.default is None and column.server_default is None and column.computed is None
            )
        else:
            assert column.default is None and column.computed is None
            assert normalized(column.server_default.arg) == normalized(default)


def expected_shared_checks(table):
    """Concrete applications of Day 4 common conventions, not model introspection."""
    checks = []
    aliases = {
        "created_by",
        "submitted_by",
        "requested_by",
        "resolved_by",
        "released_by",
        "initiated_by",
    }
    tokens = {
        "role",
        "permission",
        "correlation_id",
        "external_workflow_id",
        "authentication_subject",
        "idempotency_key",
        "rule_version",
        "graph_version",
        "dataset_version",
    }
    for column in table["columns"]:
        name, kind = column["name"], column["type"]
        if kind.startswith("NUMERIC"):
            checks.append(f"{name} <> 'NaN'::numeric")
        elif kind == "TEXT":
            if (
                name.endswith("_id") and name not in {"correlation_id", "external_workflow_id"}
            ) or name in aliases:
                checks.append(f"length(btrim({name})) > 0 AND length({name}) <= 64")
            elif name in table["pk"] or name in tokens or name.endswith("_rule_version"):
                checks.append(f"length(btrim({name})) > 0")
            if "64 lowercase hexadecimal characters" in column["rule"]:
                checks.append(f"{name} ~ '^[0-9a-f]{{64}}$'")
        elif kind == "JSONB" and not any(f"jsonb_typeof({name})" in sql for sql in table["checks"]):
            shape = (
                "array" if name in {"part_ids", "source_evidence", "parts", "lines"} else "object"
            )
            checks.append(f"jsonb_typeof({name}) = '{shape}'")
    return checks


@pytest.mark.parametrize("name", SCHEMA_TABLES)
def test_exact_table_and_common_checks_match_day4(approved, name):
    checks = [key for key in orm_table(name).constraints if isinstance(key, CheckConstraint)]
    wanted = approved[name]["checks"] + expected_shared_checks(approved[name])
    assert {normalized(check.sqltext) for check in checks} == {normalized(sql) for sql in wanted}
    assert len(checks) == len(wanted)
    assert all(check.name for check in checks)


def test_every_documented_index_matches_with_no_redundant_indexes():
    text = (ROOT / "docs/database/relationships_and_constraints.md").read_text()
    section = text.split("### 5.2 Additional indexes", 1)[1].split("## 6.", 1)[0]
    expectations = {}
    for line in section.splitlines():
        if not line.startswith(("| ix_", "| uq_")):
            continue
        name, table, columns, kind, unique, predicate, *_ = [
            s.strip() for s in line.split("|")[1:-1]
        ]
        assert kind == "B-tree"
        expectations[name] = (
            table,
            tuple(columns.split(", ")),
            unique == "Yes",
            None if predicate == "—" else normalized(predicate),
        )
    found = {}
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            columns = tuple(
                expr.name if hasattr(expr, "name") else str(expr) for expr in index.expressions
            )
            where = index.dialect_options["postgresql"]["where"]
            found[index.name] = (
                table.name,
                columns,
                index.unique,
                None if where is None else normalized(where),
            )
            assert index.dialect_options["postgresql"]["using"] == "btree"
    # TD-012 indexes (revision 20261110_01), not in the Day 4 document.
    expectations["ix_user_customer_bindings_customer_id"] = (
        TD012_TABLE,
        ("customer_id",),
        False,
        None,
    )
    expectations["uq_user_customer_bindings_active_user"] = (
        TD012_TABLE,
        ("user_id",),
        True,
        normalized("revoked_at IS NULL"),
    )
    # Phase 3.5 indexes (20261111_02/03).
    expectations["ix_rfq_assignments_active_user"] = (
        "rfq_assignments", ("user_id", "rfq_id"), False, normalized("revoked_at IS NULL"),
    )  # fmt: skip
    expectations["uq_token_revocations_token_sha256"] = (
        "token_revocations", ("token_sha256",), True, normalized("token_sha256 IS NOT NULL"),
    )  # fmt: skip
    expectations["ix_token_revocations_user_not_before"] = (
        "token_revocations", ("user_id", "not_before"), False, normalized("not_before IS NOT NULL"),
    )  # fmt: skip
    assert found == expectations and len(found) == 19


@pytest.mark.parametrize("name", APPROVED_TABLES)
def test_each_fk_has_a_resolved_bidirectional_relationship(approved, name):
    table = orm_table(name)
    mapper = next(inspect(cls) for cls in MODEL_CLASSES if cls.__table__ is table)
    read_only = {
        ("quotes", ("quote_id", "current_version")),
        ("quotes", ("quote_id", "approved_version")),
        ("approval_history", ("approval_request_id", "quote_id", "quote_version")),
        ("audit_events", ("quote_id", "quote_version")),
    }
    for fk in approved[name]["fks"]:
        matching = [
            rel
            for rel in mapper.relationships
            if rel.direction is MANYTOONE
            and rel.mapper.local_table.name == fk["target"]
            and {col.name for col in rel.local_columns} == set(fk["local"])
        ]
        assert len(matching) == 1
        relationship = matching[0]
        assert {
            (local.name, remote.name) for local, remote in relationship.local_remote_pairs
        } == set(zip(fk["local"], fk["remote"]))
        assert relationship.uselist is False
        assert relationship.viewonly is ((name, fk["local"]) in read_only)
        reverse = relationship.mapper.relationships[relationship.back_populates]
        assert reverse.back_populates == relationship.key
        assert reverse.mapper is mapper and reverse.viewonly == relationship.viewonly
        one_child = any(
            set(key) <= set(fk["local"])
            for key in [approved[name]["pk"], *approved[name]["unique"]]
        )
        assert reverse.uselist is not one_child
        for rel in [relationship, reverse]:
            assert rel.lazy == "raise"
            assert not rel.cascade.delete and not rel.cascade.delete_orphan
        if not reverse.viewonly:
            assert reverse.passive_deletes == "all"
    for rel in mapper.relationships:
        assert rel.mapper.registry is Base.registry
        assert rel.back_populates is not None


def test_relationship_count_and_core_ownership_paths():
    assert sum(len(inspect(model).relationships) for model in MODEL_CLASSES) == 128
    assert inspect(RFQ).relationships.quote.uselist is False
    assert inspect(Quote).relationships.versions.uselist is True
    assert inspect(QuoteVersion).relationships.line_items.uselist is True
    assert (
        inspect(MaterialInventory).relationships.thickness_option.mapper.class_
        is MaterialThicknessOption
    )
    assert inspect(MaterialThicknessOption).relationships.material.mapper.class_ is Material
    assert inspect(ApprovalHistory).relationships.request.viewonly
    assert inspect(AuditEvent).relationships.version_record.viewonly


def test_quote_cycle_and_version_pinning_remain_database_constraints():
    quote = orm_table("quotes")
    deferred = [fk for fk in quote.foreign_key_constraints if fk.deferrable]
    assert len(deferred) == 2 and all(
        fk.use_alter and fk.initially == "DEFERRED" for fk in deferred
    )
    for fk in deferred:
        ddl = str(AddConstraint(fk).compile(dialect=PG))
        assert "DEFERRABLE INITIALLY DEFERRED" in ddl and "ON DELETE RESTRICT" in ddl
    assert {
        tuple(key.columns.keys())
        for key in orm_table("quote_versions").constraints
        if isinstance(key, UniqueConstraint)
    } == {("quote_id", "version")}
    assert orm_table("quote_versions").c.total_usd.nullable
    assert orm_table("quote_versions").c.manufacturing_rule_version.nullable
    assert orm_table("approval_history").c.approval_request_id.nullable


@pytest.mark.parametrize("name", SCHEMA_TABLES)
def test_postgresql_ddl_compiles_offline(name):
    table = orm_table(name)
    ddl = str(CreateTable(table).compile(dialect=PG))
    assert f"CREATE TABLE {table.schema}.{table.name}" in ddl
    for fk in table.foreign_key_constraints:
        if fk.use_alter:
            assert str(AddConstraint(fk).compile(dialect=PG)).startswith("ALTER TABLE")
    for index in table.indexes:
        assert "INDEX" in str(CreateIndex(index).compile(dialect=PG))
    assert all(constraint.name for constraint in table.constraints)


def test_generated_columns_defaults_and_utc_do_not_add_side_effects():
    for name in ["available_kg"]:
        assert orm_table("material_inventory").c[name].computed.persisted
    for name in ["available_hours", "remaining_hours", "utilization_fraction"]:
        expression = str(orm_table("production_capacity").c[name].computed.sqltext)
        assert "available_hours" not in expression and "remaining_hours" not in expression
    for name in ["idempotency_records", "audit_events", "workflow_runs"]:
        checks = " ".join(
            str(key.sqltext)
            for key in orm_table(name).constraints
            if isinstance(key, CheckConstraint)
        )
        assert "length(correlation_id) <= 64" not in checks
    assert orm_table("workflow_runs").c.updated_at.onupdate is None
    assert not any(
        col.name in ["tenant_id", "password"]
        for table in Base.metadata.tables.values()
        for col in table.c
    )


def fixture_values(spec):
    """Transient constructor fixtures: typed scalar values, never seed/import operations."""
    values = {}
    for column in spec["columns"]:
        name, kind = column["name"], column["type"]
        if column["default"].startswith("generated: "):
            continue
        if column["nullable"]:
            values[name] = None
        elif kind == "UUID":
            values[name] = UUID("00000000-0000-4000-8000-000000000001")
        elif kind == "DATE":
            values[name] = date(2026, 11, 2)
        elif kind == "TIMESTAMPTZ":
            values[name] = datetime(2026, 11, 2, tzinfo=UTC)
        elif kind.startswith("NUMERIC"):
            values[name] = Decimal("1")
        elif kind in ["INTEGER", "BIGINT"]:
            values[name] = 1
        elif kind == "BOOLEAN":
            values[name] = True
        elif kind == "JSONB":
            values[name] = [] if name in ["part_ids", "source_evidence", "parts", "lines"] else {}
        elif name.endswith("sha256") or name == "sha256":
            values[name] = "0" * 64
        else:
            values[name] = name + "-fixture"
    # Local approved vocabularies; no claim of backend business-policy validation.
    for sql in spec["checks"]:
        match = re.match(r"(\w+) IN \('([^']+)'", sql)
        if match and match[1] in values and values[match[1]] is not None:
            values[match[1]] = match[2]
    for column in spec["columns"]:
        default = column["default"]
        if default.startswith("'") and default.endswith("'"):
            values[column["name"]] = default[1:-1]
    if "quantity_kg" in values:
        values["quantity_kg"] = Decimal("100")
    if "reserved_kg" in values:
        values["reserved_kg"] = Decimal("0")
    if "nominal_hours" in values:
        values.update(
            nominal_hours=Decimal("80"), downtime_hours=Decimal("6"), scheduled_hours=Decimal("0")
        )
    if "weekly_nominal_hours" in values:
        values.update(weekly_nominal_hours=Decimal("80"), downtime_hours_per_week=Decimal("6"))
    if "expires_at" in values:
        values["expires_at"] = datetime(2026, 11, 3, tzinfo=UTC)
    if "required_role" in values:
        values["required_role"] = "sales_manager"
    if "unit" in values:
        values["unit"] = "kg"
    if "stock_form" in values:
        values["stock_form"] = "sheet"
    if "is_trusted_instruction" in values:
        values["is_trusted_instruction"] = False
    if "required_when" in values:
        values["required_when"] = "always"
    return values


@pytest.mark.parametrize("name", SCHEMA_TABLES)
def test_each_model_constructs_transient_typed_fixtures(approved, name):
    model = next(cls for cls in MODEL_CLASSES if cls.__table__.name == name)
    values = fixture_values(approved[name])
    instance = model(**values)
    assert inspect(instance).transient
    assert inspect(instance).session is None
    for key, value in values.items():
        assert getattr(instance, key) == value
    for column in approved[name]["columns"]:
        if column["type"].startswith("NUMERIC") and values.get(column["name"]) is not None:
            assert isinstance(getattr(instance, column["name"]), Decimal)


def test_actual_source_identifiers_and_json_snapshots_construct_without_conversion():
    import csv

    data = ROOT / "data/rfq_synthetic_data"
    with (data / "master_data/customers.csv").open() as source:
        customer_row = next(csv.DictReader(source))
    customer_row.update(
        credit_limit_usd=Decimal(customer_row["credit_limit_usd"]),
        credit_exposure_usd=Decimal(customer_row["credit_exposure_usd"]),
        created_date=date.fromisoformat(customer_row["created_date"]),
    )
    customer = Customer(**customer_row)
    with (data / "transactional_data/quote_line_items.csv").open() as source:
        source_line = next(csv.DictReader(source))
    line = QuoteLineItem(
        **{
            key: value
            for key, value in source_line.items()
            if key
            not in [
                "quote_version",
                "line_number",
                "quantity",
                "line_total_usd",
                "part_json",
                "cost_breakdown_json",
            ]
        },
        quote_version=int(source_line["quote_version"]),
        line_number=int(source_line["line_number"]),
        quantity=int(source_line["quantity"]),
        line_total_usd=Decimal(source_line["line_total_usd"]),
        part_json=json.loads(source_line["part_json"]),
        cost_breakdown_json=json.loads(source_line["cost_breakdown_json"]),
    )
    assert customer.customer_id == "CUST-001"
    assert line.quote_id == "HQUOTE-0001"
    assert isinstance(line.line_total_usd, Decimal)
    assert isinstance(line.cost_breakdown_json["quoted_unit_price_usd"], str)


def test_association_back_populates_and_one_to_one_ownership_in_memory():
    customer = Customer(customer_id="CUST-FIXTURE")
    contact = Contact(contact_id="CONT-FIXTURE", customer=customer)
    assert customer.contacts == [contact]
    rfq = RFQ(rfq_id="RFQ-FIXTURE", customer=customer)
    quote = Quote(quote_id="QUOTE-FIXTURE", rfq=rfq, current_version=1)
    version = QuoteVersion(quote_version_id="QVERSION-FIXTURE", quote=quote, version=1)
    assert rfq.quote is quote and quote.versions == [version]
    assert version.quote is quote and contact.customer is customer
    sql = select(Quote).options(selectinload(Quote.versions), joinedload(Quote.rfq))
    assert "business.quotes" in str(sql.compile(dialect=PG))


def test_fresh_module_import_and_metadata_discovery_perform_no_database_io():
    script = """
from unittest.mock import patch
from contextlib import ExitStack
import sqlalchemy
import sqlalchemy.ext.asyncio
from sqlalchemy.orm import configure_mappers

def forbidden(*args, **kwargs):
    raise AssertionError('Model import attempted engine creation, connection or physical DDL')

with ExitStack() as stack:
    for name in ['sqlalchemy.create_engine', 'sqlalchemy.ext.asyncio.create_async_engine',
                 'sqlalchemy.engine.Engine.connect', 'sqlalchemy.ext.asyncio.AsyncEngine.connect',
                 'sqlalchemy.MetaData.create_all', 'sqlalchemy.Table.create',
                 'socket.create_connection', 'socket.socket.connect']:
        stack.enter_context(patch(name, side_effect=forbidden))
    import rfq_copilot.models as models
    configure_mappers()
    assert len(models.Base.metadata.tables) == 36
    for cls in models.MODEL_CLASSES:
        assert cls.metadata is models.Base.metadata
    print('36 models registered without database I/O')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, text=True, capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "36 models registered without database I/O"


@pytest.mark.asyncio
async def test_existing_async_session_accepts_models_without_connecting():
    from rfq_copilot.config import DatabaseConfig
    from rfq_copilot.database import Database

    config = DatabaseConfig.from_env(
        environ={"DATABASE_URL": "postgresql+asyncpg://example:example@127.0.0.1:1/example_dev"}
    )
    async with Database(config) as database:
        async with database.session() as session:
            customer = Customer(customer_id="CUST-TRANSIENT")
            session.add(customer)
            assert inspect(customer).pending
            assert session.get_bind() is database.engine.sync_engine
        assert inspect(customer).transient
        assert database.engine.pool.checkedout() == 0


def test_all_domain_modules_import_into_same_metadata():
    names = [
        "base",
        "rules",
        "identity",
        "customers",
        "manufacturing",
        "supply",
        "rfqs",
        "quotations",
        "approvals",
        "reliability",
    ]
    for name in names:
        module = importlib.import_module("rfq_copilot.models." + name)
        assert module.Base.metadata is Base.metadata
