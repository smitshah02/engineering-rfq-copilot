"""Cross-record and nested snapshot invariants that SQL FKs alone cannot enforce."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from rfq_copilot.seeding.transform import calendar_date, decimal_value, integer, require

COST_FIELDS = frozenset(
    {
        "blank_area_m2",
        "finish_cost_per_unit_usd",
        "labor_cost_per_unit_usd",
        "line_total_usd",
        "material_allowance_kg_per_unit",
        "material_cost_per_unit_usd",
        "net_material_kg_per_unit",
        "operations",
        "packaging_cost_per_unit_usd",
        "part_id",
        "pricing_rule_version",
        "quantity",
        "quoted_unit_price_usd",
        "setup_cost_per_line_usd",
        "total_manufacturing_cost_usd",
        "variable_cost_per_unit_usd",
    }
)


def validate_part(part, plan, materials, finishes):
    from rfq_copilot.seeding.mapping import validate_schema

    schema = plan.bundle.json("contracts/tool_schemas.json")["tools"]["calculate_quote"][
        "input_schema"
    ]["$defs"]["Part"]
    validate_schema(part, schema, "quote part")
    require(
        set(part) <= set(schema["properties"]) | {"bend_count", "requested_operations"},
        "quote part",
        "unapproved snapshot field",
    )
    rules = plan.bundle.json("config/manufacturing_rules.json")
    limits = rules["limits"]
    material = materials.get(part["material_id"])
    require(
        material is not None and part["finish_id"] in finishes,
        "quote part",
        "unresolved catalog reference",
    )
    for field in rules["mandatory_fields"]:
        require(part.get(field) is not None, "quote part", "missing manufacturing field")
    for field, bounds in limits.items():
        if isinstance(bounds, list) and field in part:
            value = decimal_value(part[field], "quote part:" + field, 12, 3)
            require(
                Decimal(bounds[0]) <= value <= Decimal(bounds[1]),
                "quote part:" + field,
                "outside manufacturing limits",
            )
    if part["part_type"] in rules["height_required"]:
        require(part.get("height_mm") is not None, "quote part", "height required")
    if part["part_type"] in rules["weld_length_required"]:
        require(part.get("weld_length_mm") is not None, "quote part", "weld length required")
    for field, maximum in [
        ("hole_count", limits["hole_count_max"]),
        ("bend_count", limits["bends_max"]),
    ]:
        if field in part:
            require(
                0 <= integer(part[field], "quote part:" + field) <= maximum,
                "quote part:" + field,
                "outside scalar manufacturing limit",
            )
    require(
        Decimal(str(part["tolerance_mm"])) >= limits["min_tolerance_mm"],
        "quote part",
        "unsupported tolerance",
    )
    thickness = Decimal(str(part["thickness_mm"]))
    require(
        (part["material_id"], thickness)
        in {(r["material_id"], r["thickness_mm"]) for r in plan.rows["material_thickness_options"]},
        "quote part",
        "unsupported material thickness",
    )
    require(
        (part["material_id"], part["finish_id"])
        in {(r["material_id"], r["finish_id"]) for r in plan.rows["material_finish_compatibility"]},
        "quote part",
        "incompatible finish",
    )
    if part.get("bend_radius_mm") is not None:
        radius = decimal_value(part["bend_radius_mm"], "quote part:bend_radius_mm", 12, 3)
        require(
            radius >= thickness * limits["min_bend_radius_factor"],
            "quote part",
            "bend radius too small",
        )
    if "requested_operations" in part:
        require(
            isinstance(part["requested_operations"], list), "quote part", "expected operation array"
        )
        supported = {
            r["operation_id"]
            for r in plan.rows["material_supported_operations"]
            if r["material_id"] == part["material_id"]
        }
        require(
            set(part["requested_operations"]) <= supported,
            "quote part",
            "unsupported requested operation",
        )


def validate_business(plan):
    rows = plan.rows
    materials = {r["material_id"]: r for r in rows["materials"]}
    finishes = {r["finish_id"]: r for r in rows["finishes"]}
    categories = {
        (r["finish_id"], r["material_category"]) for r in rows["finish_material_categories"]
    }
    for pair in rows["material_finish_compatibility"]:
        require(
            (pair["finish_id"], materials[pair["material_id"]]["category"]) in categories,
            "material finish",
            "category incompatible",
        )
    pricing = {
        r["rule_version"] for r in rows["business_rule_versions"] if r["rule_kind"] == "pricing"
    }
    for record in [*rows["materials"], *rows["finishes"]]:
        require(record["pricing_rule_version"] in pricing, "catalog", "wrong rule kind")
    start = calendar_date(plan.bundle.generation["schedule_start"], "capacity")
    weeks = {start + timedelta(weeks=w) for w in range(plan.bundle.generation["planning_weeks"])}
    capacity_weeks = defaultdict(set)
    for record in rows["production_capacity"]:
        require(record["week_start"] in weeks, "capacity", "outside baseline schedule")
        capacity_weeks[record["workcentre_id"]].add(record["week_start"])
    require(
        all(capacity_weeks[r["workcentre_id"]] == weeks for r in rows["workcentres"]),
        "capacity",
        "incomplete workcentre horizon",
    )
    for customer in rows["customers"]:
        primaries = [
            r
            for r in rows["contacts"]
            if r["customer_id"] == customer["customer_id"] and r["is_primary"]
        ]
        require(len(primaries) <= 1, "contacts", "duplicate primary contact")
    allowed_types = plan.bundle.json("config/manufacturing_rules.json")["allowed_part_types"]
    for route in rows["operation_routing"]:
        require(route["part_type"] in allowed_types, "routing", "unknown part type")

    rfqs = {r["rfq_id"]: r for r in rows["rfqs"]}
    users = {r["user_id"]: r for r in rows["users"]}
    versions = defaultdict(list)
    lines = defaultdict(list)
    events = defaultdict(list)
    for record in rows["rfqs"]:
        parts = record["part_ids"]
        require(
            isinstance(parts, list)
            and all(isinstance(p, str) and p.strip() and len(p) <= 64 for p in parts),
            "rfqs",
            "invalid part references",
        )
        require(len(set(parts)) == len(parts), "rfqs", "duplicate part reference")
    for record in rows["quote_versions"]:
        versions[record["quote_id"]].append(record)
    for record in rows["approval_history"]:
        events[record["quote_id"]].append(record)
    for line in rows["quote_line_items"]:
        part, cost = line["part_json"], line["cost_breakdown_json"]
        require(
            isinstance(part, dict) and isinstance(cost, dict), "quote line", "expected JSON objects"
        )
        validate_part(part, plan, materials, finishes)
        require(set(cost) == COST_FIELDS, "quote cost", "unapproved/missing snapshot fields")
        for value in [part, cost]:
            require(
                value["part_id"] == line["part_id"]
                and type(value["quantity"]) is int
                and value["quantity"] == line["quantity"],
                "quote line",
                "nested identifier/quantity mismatch",
            )
        require(
            decimal_value(cost["line_total_usd"], "quote cost", 18, 2) == line["line_total_usd"],
            "quote line",
            "nested line total mismatch",
        )
        require(cost["pricing_rule_version"] in pricing, "quote cost", "unresolved pricing rule")
        for key in COST_FIELDS - {"operations", "part_id", "pricing_rule_version", "quantity"}:
            require(
                isinstance(cost[key], str), "quote cost", "decimal evidence must remain a string"
            )
            # These JSON strings intentionally retain more precision than inventory columns.
            value = Decimal(cost[key])
            require(value.is_finite() and value >= 0, "quote cost", "invalid decimal evidence")
        require(
            isinstance(cost["operations"], list)
            and all(isinstance(v, str) for v in cost["operations"]),
            "quote cost",
            "invalid operations",
        )
        supported = {
            r["operation_id"]
            for r in rows["material_supported_operations"]
            if r["material_id"] == part["material_id"]
        }
        require(set(cost["operations"]) <= supported, "quote cost", "unresolved operation")
        lines[(line["quote_id"], line["quote_version"])].append(line)

    for quote in rows["quotes"]:
        key = quote["quote_id"]
        current = [v for v in versions[key] if v["is_current"]]
        require(
            len(current) == 1
            and current[0]["version"]
            == quote["current_version"]
            == max(v["version"] for v in versions[key]),
            "quotes",
            "current version/flag mismatch",
        )
        require(
            current[0]["approval_valid"] == (quote["approved_version"] is not None),
            "quotes",
            "approval flag mismatch",
        )
        current_lines = lines[(key, quote["current_version"])]
        require(bool(current_lines), "quotes", "missing current lines")
        require(
            sorted(r["line_number"] for r in current_lines)
            == list(range(1, len(current_lines) + 1)),
            "quote lines",
            "noncontiguous ordinals",
        )
        require(
            sum((r["line_total_usd"] for r in current_lines), Decimal(0))
            == current[0]["total_usd"],
            "quotes",
            "current line sum differs",
        )
        require(
            {r["part_id"] for r in current_lines} == set(rfqs[quote["rfq_id"]]["part_ids"]),
            "quote lines",
            "RFQ part references differ",
        )
        for version in versions[key]:
            if not version["is_current"]:
                require(
                    not lines[(key, version["version"])],
                    "quote versions",
                    "unexpected older priced lines",
                )
        for event in events[key]:
            require(
                event["quote_version"] == quote["current_version"]
                and event["action_at"] >= quote["created_at"],
                "approval history",
                "version/chronology mismatch",
            )
            if event["action"] == "submitted":
                require(
                    event["actor_id"] == quote["submitted_by"],
                    "approval history",
                    "submitter mismatch",
                )
            else:
                require(
                    event["actor_id"] == quote["approver_id"]
                    and event["actor_id"] != quote["created_by"]
                    and users[event["actor_id"]]["role"] == "sales_manager",
                    "approval history",
                    "review identity mismatch",
                )
                if event["action"] == "approved":
                    require(
                        event["action_at"] == quote["approval_timestamp"],
                        "approval history",
                        "approval timestamp mismatch",
                    )
                if event["action"] == "rejected":
                    # Event prose and the header summary are separate supplied facts.
                    require(
                        quote["rejection_reason"] is not None,
                        "approval history",
                        "missing rejection summary",
                    )
        for action, expected in [
            ("submitted", quote["submitted_by"] is not None),
            ("approved", quote["approved_version"] is not None),
            ("rejected", quote["status"] == "rejected"),
        ]:
            require(
                sum(e["action"] == action for e in events[key]) == int(expected),
                "approval history",
                "header/event summary differs",
            )
