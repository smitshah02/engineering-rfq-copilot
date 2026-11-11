"""Day 4 source projections, historical joins and metadata-derived insertion order."""

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from jsonschema import Draft202012Validator
from sqlalchemy import Boolean, Date, DateTime, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from rfq_copilot.models import Base
from rfq_copilot.seeding.sources import (
    CSV_SOURCES,
    GENERATION,
    RFQS,
    RULE_SOURCES,
    SourceBundle,
    read_csv,
)
from rfq_copilot.seeding.transform import calendar_date, convert, json_text, json_value, require

TABLES = {t.name: t for t in Base.metadata.tables.values()}
EMPTY_TABLES = frozenset(
    {
        "rfq_line_items",
        "rfq_attachments",
        "pricing_calculations",
        "approval_requests",
        "idempotency_records",
        "audit_events",
        "workflow_runs",
        "user_customer_bindings",  # TD-012: provisioned explicitly, never seeded
        "rfq_assignments",  # Phase 3.5 (20261111_02): provisioned explicitly, never seeded
        "token_revocations",  # Phase 3.5 (20261111_03): operator-written, never seeded
    }
)
DERIVED_COUNTS = {
    "roles": 6,
    "permissions": 12,
    "material_thickness_options": 90,
    "material_finish_compatibility": 50,
    "material_supported_operations": 90,
    "finish_material_categories": 19,
    "business_rule_versions": 9,
    "data_snapshots": 2,
}
EXPECTED_COUNTS = {
    **dict.fromkeys(EMPTY_TABLES, 0),
    **DERIVED_COUNTS,
    **{t: count for t, _, _, count in CSV_SOURCES},
    "rfqs": 80,
}


def validate_schema(record, schema, location):
    require(Draft202012Validator(schema).is_valid(record), location, "source schema violation")


def dependency_order() -> tuple[str, ...]:
    """Topologically sort immediate FKs; only the two approved quote pointers defer."""
    deferred = []
    dependencies = {}
    for name, table in TABLES.items():
        dependencies[name] = set()
        for fk in table.foreign_key_constraints:
            if fk.deferrable:
                require(
                    name == "quotes" and fk.initially == "DEFERRED", name, "unexpected deferred FK"
                )
                deferred.append(tuple(e.parent.name for e in fk.elements))
            else:
                dependencies[name].add(fk.referred_table.name)
    require(
        set(deferred) == {("quote_id", "current_version"), ("quote_id", "approved_version")},
        "metadata",
        "quote cycle contract differs",
    )
    order = []
    while dependencies:
        ready = sorted(n for n, parents in dependencies.items() if not parents)
        require(bool(ready), "metadata", "immediate FK cycle")
        for name in ready:
            order.append(name)
            del dependencies[name]
        for parents in dependencies.values():
            parents.difference_update(ready)
    return tuple(order)


@dataclass(frozen=True)
class SeedPlan:
    rows: dict[str, list[dict]] = field(repr=False)
    sources: dict[str, list[str]]
    bundle: SourceBundle = field(repr=False)
    snapshot_dir: Path
    order: tuple[str, ...]

    def summary(self):
        return {
            **self.bundle.integrity,
            "validated_records": sum(map(len, self.rows.values())),
            "source_business_records": 811,
            "dependency_order": list(self.order),
            "tables": {
                TABLES[n].fullname: {
                    "expected": len(self.rows[n]),
                    "sources": self.sources[n],
                    "classification": "intentionally_empty"
                    if n in EMPTY_TABLES
                    else "deterministic_transformation"
                    if n in DERIVED_COUNTS or n == "quote_versions"
                    else "direct_with_typed_conversion",
                }
                for n in self.order
            },
        }


def build_plan(bundle: SourceBundle, snapshot_dir: Path) -> SeedPlan:
    require(
        not snapshot_dir.resolve().is_relative_to(bundle.source_directory),
        "snapshots",
        "directory must be outside frozen sources",
    )
    rows = {name: [] for name in TABLES}
    sources = {name: [] for name in TABLES}
    entity_contract = bundle.json("contracts/entity_schemas.json")
    schemas = entity_contract["entity_json_schemas"]
    raw_rows = {}
    for target, entity, path, count in CSV_SOURCES:
        schema = (
            schemas[entity]
            if entity
            else {
                "type": "object",
                "required": ["role", "permission", "allowed"],
                "properties": {k: {"type": "string"} for k in ["role", "permission", "allowed"]},
                "additionalProperties": False,
            }
        )
        source_rows = read_csv(bundle.raw[path], list(schema["properties"]), path)
        require(len(source_rows) == count, path, "unexpected v1.1.0 source count")
        for number, record in enumerate(source_rows, 2):
            validate_schema(record, schema, f"{path}:{number}")
        raw_rows[target] = source_rows
        sources[target] = [path]

    for kind, path, version in RULE_SOURCES:
        document = bundle.json(path)
        require(isinstance(document, dict), path, "expected object")
        if version:
            require(document.get("version") == version, path, "rule version mismatch")
        digest = hashlib.sha256(bundle.raw[path]).hexdigest()
        rows["business_rule_versions"].append(
            {
                "rule_version": version or f"{kind}@sha256:{digest}",
                "rule_kind": kind,
                "sha256": digest,
                "document": document,
                "source_path": "data/rfq_synthetic_data/" + path,
            }
        )
    sources["business_rule_versions"] = [p for _, p, _ in RULE_SOURCES]
    require(entity_contract.get("schema_version") == "1.0", "entity contract", "version mismatch")
    require(
        bundle.json("contracts/tool_schemas.json").get("contract_version") == "1.1",
        "tool contract",
        "version mismatch",
    )
    require(
        bundle.json("contracts/agent_coordination_policy.json").get("version") == "1.1",
        "coordination contract",
        "version mismatch",
    )
    require(
        bundle.json("contracts/agent_handoff_schema.json")["properties"]["schema_version"]["const"]
        == "1.1",
        "handoff contract",
        "version mismatch",
    )
    # The Day 4 business_constraints version label belongs to the handoff schema.
    require(
        "version" not in bundle.json("contracts/business_constraints.json"),
        "business constraints",
        "unexpected version field",
    )

    for name, source_rows in raw_rows.items():
        if name == "quotes":
            continue
        for number, source in enumerate(source_rows, 2):
            record = {}
            for key, value in source.items():
                if key in TABLES[name].c:
                    record[key] = convert(value, TABLES[name].c[key], f"{name}:{number}:{key}")
            rows[name].append(record)

    grants = raw_rows["role_permissions"]
    rows["roles"] = [
        {"role": v}
        for v in sorted({r["role"] for r in grants} | {r["role"] for r in raw_rows["users"]})
    ]
    rows["permissions"] = [{"permission": v} for v in sorted({r["permission"] for r in grants})]
    sources["roles"] = ["master_data/users.csv", "master_data/roles_permissions.csv"]
    sources["permissions"] = ["master_data/roles_permissions.csv"]
    projections = (
        (
            "materials",
            "supported_thickness_mm",
            "material_thickness_options",
            "material_id",
            "thickness_mm",
        ),
        (
            "materials",
            "compatible_finishes",
            "material_finish_compatibility",
            "material_id",
            "finish_id",
        ),
        (
            "materials",
            "supported_operations",
            "material_supported_operations",
            "material_id",
            "operation_id",
        ),
        (
            "finishes",
            "material_categories",
            "finish_material_categories",
            "finish_id",
            "material_category",
        ),
    )
    for parent, field_name, target, owner, value_column in projections:
        sources[target] = sources[parent]
        for source in raw_rows[parent]:
            values = json_value(source[field_name], f"{parent}:{field_name}")
            require(isinstance(values, list) and bool(values), target, "expected nonempty array")
            for value in values:
                rows[target].append(
                    {
                        owner: source[owner],
                        value_column: convert(value, TABLES[target].c[value_column], target),
                    }
                )

    for kind, table in [("inventory", "material_inventory"), ("capacity", "production_capacity")]:
        path = sources[table][0]
        digest = hashlib.sha256(bundle.raw[path]).hexdigest()
        token = f"{kind}-v{bundle.generation['version']}-{bundle.generation['reference_date']}"
        rows["data_snapshots"].append(
            {
                "snapshot_id": token,
                "snapshot_kind": kind,
                "dataset_version": bundle.generation["version"],
                "as_of_date": calendar_date(bundle.generation["reference_date"], GENERATION),
                "sha256": digest,
                "artifact_ref": str(snapshot_dir.resolve() / f"{kind}-{digest}.csv"),
            }
        )
        for record in rows[table]:
            record.update(snapshot_id=token, row_version=1)
            if kind == "inventory":
                require(
                    record["snapshot_date"] == rows["data_snapshots"][-1]["as_of_date"],
                    table,
                    "snapshot date differs",
                )
                require(
                    record["available_kg"] == record["quantity_kg"] - record["reserved_kg"],
                    table,
                    "inventory arithmetic differs",
                )
            else:
                available = record["nominal_hours"] - record["downtime_hours"]
                require(
                    record["available_hours"] == available
                    and record["remaining_hours"] == available - record["scheduled_hours"],
                    table,
                    "capacity arithmetic differs",
                )
                utilization = (
                    (record["scheduled_hours"] / available).quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    )
                    if available
                    else None
                )
                require(
                    record["utilization_fraction"] == utilization,
                    table,
                    "utilization rounding differs",
                )
    sources["data_snapshots"] = [
        "operations_data/material_inventory.csv",
        "operations_data/production_capacity.csv",
        GENERATION,
    ]

    historical_rfqs = bundle.json(RFQS)
    require(
        isinstance(historical_rfqs, list) and len(historical_rfqs) == 80,
        RFQS,
        "unexpected RFQ shape/count",
    )
    for number, source in enumerate(historical_rfqs, 1):
        validate_schema(source, schemas["historical_rfq"], f"{RFQS}:{number}")
        record = {
            k: convert(v, TABLES["rfqs"].c[k], f"rfqs:{number}:{k}") for k, v in source.items()
        }
        record.update(status="archived", created_by=None, row_version=1)
        rows["rfqs"].append(record)
    sources["rfqs"] = [RFQS]

    headers = {}
    payload_fields = [
        "currency",
        "total_usd",
        "pricing_rule_version",
        "expiry_date",
        "estimated_lead_business_days",
    ]
    for number, source in enumerate(raw_rows["quotes"], 2):
        record = {}
        for key, value in source.items():
            if key in payload_fields:
                continue
            target = {
                "version": "current_version",
                "idempotency_key": "legacy_idempotency_key",
            }.get(key, key)
            record[target] = convert(value, TABLES["quotes"].c[target], f"quotes:{number}:{target}")
        record.update(row_version=1, released_by=None, released_at=None)
        rows["quotes"].append(record)
        require(record["quote_id"] not in headers, "quotes", "duplicate source identifier")
        headers[record["quote_id"]] = (record, source)
    sources["quote_versions"].append(sources["quotes"][0])
    for record in rows["quote_versions"]:
        require(record["quote_id"] in headers, "quote_versions", "unresolved quote")
        header, source = headers[record["quote_id"]]
        current = record["version"] == header["current_version"]
        record.update(
            currency="USD", snapshot_completeness="priced_partial" if current else "metadata_only"
        )
        if current:
            for key in payload_fields:
                record[key] = convert(
                    source[key], TABLES["quote_versions"].c[key], "quote_versions:" + key
                )

    # Explicit absent historical facts, never guessed policy/actor/snapshot pins.
    for name, records in rows.items():
        for record in records:
            for col in TABLES[name].c:
                if col.name not in record:
                    if col.nullable:
                        record[col.name] = None
                    else:
                        require(
                            col.name == "registered_at" and col.server_default is not None,
                            name,
                            "unmapped required column",
                        )
        require(len(records) == EXPECTED_COUNTS[name], name, "unexpected normalized record count")
    plan = SeedPlan(rows, sources, bundle, snapshot_dir, dependency_order())
    validate_plan(plan)
    return plan


def key_for(row: dict, columns) -> tuple:
    return tuple(row[c.name] for c in columns)


def validate_keys(plan: SeedPlan) -> None:
    for name, table in TABLES.items():
        constraints = [
            table.primary_key,
            *[c for c in table.constraints if isinstance(c, UniqueConstraint)],
        ]
        for constraint in constraints:
            seen = set()
            for record in plan.rows[name]:
                key = key_for(record, constraint.columns)
                if any(v is None for v in key):
                    continue
                require(key not in seen, name, "duplicate primary/natural/composite identifier")
                seen.add(key)
        for fk in table.foreign_key_constraints:
            parent = fk.referred_table.name
            targets = {tuple(r[e.column.name] for e in fk.elements) for r in plan.rows[parent]}
            for record in plan.rows[name]:
                key = tuple(record[e.parent.name] for e in fk.elements)
                if not any(v is None for v in key):
                    require(key in targets, name, "unresolved foreign-key tuple")


def validate_plan(plan: SeedPlan) -> None:
    """Validate native records again at the transaction boundary, including NOT NULL."""
    from rfq_copilot.seeding.validation import validate_business

    require(set(plan.rows) == set(TABLES), "plan", "incomplete table inventory")
    for name, table in TABLES.items():
        require(len(plan.rows[name]) == EXPECTED_COUNTS[name], name, "incomplete baseline plan")
        columns = {c.name for c in table.c if c.name != "registered_at"}
        for record in plan.rows[name]:
            require(set(record) == columns, name, "unmapped or unexpected target columns")
            for key, value in record.items():
                col = table.c[key]
                location = f"{name}:{key}"
                if value is None:
                    require(col.nullable, location, "required value is missing")
                elif isinstance(col.type, JSONB):
                    require(
                        isinstance(value, list if key == "part_ids" else dict),
                        location,
                        "invalid JSON shape",
                    )
                    json_text(value)
                elif isinstance(col.type, DateTime):
                    require(
                        isinstance(value, datetime)
                        and value.tzinfo is not None
                        and value.utcoffset() is not None,
                        location,
                        "aware timestamp required",
                    )
                elif isinstance(col.type, Date):
                    require(type(value) is date, location, "calendar date required")
                elif isinstance(col.type, Boolean):
                    require(type(value) is bool, location, "boolean required")
                else:
                    if isinstance(col.type, Numeric):
                        require(isinstance(value, Decimal), location, "Decimal required")
                    require(convert(value, col, location) == value, location, "noncanonical value")
    validate_keys(plan)
    validate_business(plan)
