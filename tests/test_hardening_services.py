"""Phase 2.5 hardening, without a database: release invariant, review conditions,
evaluation fingerprints, snapshot guards, planning dates and the Intake boundary.

Uses the frozen v1.1.0 rule documents through the existing unit fixtures (read only).
"""

import hashlib
import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal, localcontext

import pytest
from test_capacity_service import MONDAY, ROUTING, plan, plenty, reading
from test_costing_service import STAINLESS_430
from test_database_seeding import SOURCE
from test_evaluation_service import APPROVAL, customer
from test_inventory_service import PRICING, decide, route_for, stock
from test_manufacturing_service import BRACKET, run
from test_material_service import RULES
from test_pricing_service import QUOTED
from test_quotation_service import FIRST, SECOND, quote

from rfq_copilot.canonical import canonical_json, fingerprint
from rfq_copilot.models import Customer, Material, ProductionCapacity
from rfq_copilot.repositories import SnapshotDetails, SnapshotMismatchError
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES
from rfq_copilot.seeding.rules import REGISTERED, registered_rule_row
from rfq_copilot.seeding.sources import RULE_SOURCES
from rfq_copilot.services.capacity import evaluate_capacity
from rfq_copilot.services.costing import evaluate_costs
from rfq_copilot.services.evaluation import AGENT_MAY_RELEASE_QUOTE, evaluate_outcome
from rfq_copilot.services.inventory import evaluate_inventory
from rfq_copilot.services.lead_time import (
    SnapshotSelection,
    evaluate_lead_time,
    plan_start,
)
from rfq_copilot.services.pricing import evaluate_pricing
from rfq_copilot.services.quotation import assemble_quotation

D = Decimal
# Hash-named baseline rules, keyed by kind, for comparing the manifest with RULE_SOURCES.
TRUSTED_KEY = {v[0]: k for k, v in TRUSTED_RULES.items() if "@sha256:" in k}
FLAT = replace(FIRST, part_id="FLAT-1", part_type="flat_panel", height_mm=None)
# Ids of the unit fixtures' rows: stock rows are SNAP-1, capacity rows SNAP-CAP.
INV = SnapshotDetails("SNAP-1", "inventory", "1.1.0", date(2026, 11, 2), None)
CAP = SnapshotDetails("SNAP-CAP", "capacity", "1.1.0", date(2026, 11, 2), date(2027, 1, 11))


def policy(**changes):
    return replace(APPROVAL, document={**APPROVAL.document, **changes})


def evaluate(q=None, cust=None, approval=APPROVAL, **kwargs):
    return evaluate_outcome(cust or customer(), q or quote((FIRST,)), approval, **kwargs)


# --- Finding 2: an agent can never be authorized to release a quotation ---------------


@pytest.mark.parametrize(
    "q,cust",
    [
        (None, None),
        (None, customer("on_hold")),
        (None, customer(exposure="24990")),
        ("missing", None),
    ],
)
def test_agent_release_is_never_authorized_for_a_valid_policy(q, cust):
    q = quote((replace(FIRST, tolerance_mm=None),)) if q == "missing" else q
    e = evaluate(q, cust)
    assert AGENT_MAY_RELEASE_QUOTE is False
    assert e.quote_release_authorized_for_agent is False
    assert (e.human_release_approval_required, e.approval_must_be_human) == (True, True)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"agent_forbidden_actions": ["approve_quote"]}, "release_quote"),
        ({"agent_forbidden_actions": ["release_quote"]}, "approve_quote"),
        ({"agent_forbidden_actions": []}, "human release is mandatory"),
        ({"agent_forbidden_actions": "release_quote approve_quote"}, "must be a list"),
        ({"human_release_approval_required": False}, "must be true"),
        ({"human_release_approval_required": "true"}, "must be true"),
        ({"human_release_approval_required": None}, "must be true"),
        # Contradictory: forbids the agent but declares human release unnecessary.
        (
            {"human_release_approval_required": False, "admin_does_not_bypass_approval": True},
            "true",
        ),
    ],
)
def test_policies_granting_autonomous_release_are_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        evaluate(approval=policy(**changes))


def test_missing_release_settings_are_rejected():
    document = {
        k: v
        for k, v in APPROVAL.document.items()
        if k not in ("agent_forbidden_actions", "human_release_approval_required")
    }
    with pytest.raises(ValueError):
        evaluate(approval=replace(APPROVAL, document=document))


def test_unknown_policy_keys_cannot_grant_release():
    e = evaluate(
        approval=policy(agent_release_allowed=True, quote_release_authorized_for_agent=True)
    )
    assert e.quote_release_authorized_for_agent is False


def test_draft_review_human_approval_and_release_stay_separate():
    # A clean, priced quote: drafting allowed, no manager review, but release stays human.
    e = evaluate()
    assert (e.draft_creation_allowed, e.manager_review_required) == (True, False)
    assert (e.human_release_approval_required, e.quote_release_authorized_for_agent) == (
        True,
        False,
    )


# --- Finding 5: engineering-review conditions are enforced or visibly unevaluated -----


def test_borderline_bend_radius_is_reported_unevaluated_for_bent_lines():
    e = evaluate()  # FIRST routes through bend
    assert e.unevaluated_review_conditions == ("borderline_bend_radius",)
    assert e.outcome == "quote_draft_ready_for_approval"  # labelled outcomes unchanged


def test_borderline_bend_radius_does_not_apply_without_bends():
    e = evaluate(quote((FLAT,)))
    assert "bend" not in e.quotation.lines[0].manufacturing.operations
    assert e.unevaluated_review_conditions == ()


def test_policy_without_the_condition_reports_nothing():
    conditions = ["unsupported_or_unknown_geometry", "unresolved_spec_conflict"]
    e = evaluate(approval=policy(engineering_review_conditions=conditions))
    assert e.unevaluated_review_conditions == ()


@pytest.mark.parametrize(
    "conditions,message",
    [
        (["borderline_bend_radius", "thin_wall_ratio"], "not enforced: \\['thin_wall_ratio'\\]"),
        ("borderline_bend_radius", "must be a list"),
    ],
)
def test_unknown_or_malformed_engineering_conditions_fail_closed(conditions, message):
    with pytest.raises(ValueError, match=message):
        evaluate(approval=policy(engineering_review_conditions=conditions))


def test_gate_covered_conditions_route_to_stronger_gates():
    unsupported = evaluate(quote((replace(FIRST, part_type="sphere"),)))
    assert unsupported.outcome == "unsupported_declined"
    conflict = evaluate_outcome(
        customer(), None, APPROVAL, ("QUANTITY_CONFLICT",), parts=[FIRST], line_numbers=[1]
    )
    assert conflict.outcome == "clarification_required"
    assert conflict.unevaluated_review_conditions == ()  # nothing was routed or evaluated


# --- Finding 4: evaluation fingerprint vs calculation_id ------------------------------


def quotation(parts=(FIRST,), material=STAINLESS_430, requested=None, snapshots=None):
    """A pure quotation; with snapshots, its stock and capacity rows carry those ids."""
    parts = list(parts)
    capacity_rows = None
    if snapshots is None:
        inventory = decide(parts)
    else:
        row = ("INV-1", snapshots.inventory.snapshot_id, 7, "1000")
        stocks = {(p.material_id, p.thickness_mm): stock("1000", row) for p in parts}
        inventory = decide(parts, stock_map=stocks)
        weeks = [(w, "500", snapshots.capacity.snapshot_id) for w in range(10)]
        capacity_rows = {wc: snapshot_reading(wc, weeks) for wc in plenty()}
    capacity = plan(
        parts,
        inventory=inventory,
        start_week=plan_start(inventory, MONDAY).start_week,
        capacity=capacity_rows,
    )
    lead = evaluate_lead_time(
        inventory,
        capacity,
        horizon_start=MONDAY,
        requested_delivery_date=requested,
        snapshots=snapshots,
    )
    costs = evaluate_costs(
        parts, inventory, [material], manufacturing_rules=RULES, pricing_rules=PRICING
    )
    pricing = evaluate_pricing(parts, costs, QUOTED, PRICING)
    return assemble_quotation(parts, range(1, len(parts) + 1), lead, costs, pricing)


def test_identical_evaluations_have_identical_fingerprints():
    first, second = evaluate(quotation()), evaluate(quotation())
    assert first == second and len(first.evaluation_fingerprint) == 64
    assert first.evaluation_fingerprint == second.evaluation_fingerprint


def test_material_cost_change_keeps_calculation_id_but_changes_fingerprint():
    base = evaluate(quotation())
    dearer = evaluate(
        quotation(
            material=Material(
                material_id="MAT-009", cost_usd_per_kg=D("9.99"), pricing_rule_version="price-v1.0"
            )
        )
    )
    assert base.quotation.calculation_id == dearer.quotation.calculation_id
    assert base.quotation.quote_total_usd != dearer.quotation.quote_total_usd
    assert base.evaluation_fingerprint != dearer.evaluation_fingerprint


@pytest.mark.parametrize(
    "change",
    [
        lambda: evaluate(quotation(), cust=customer(exposure="1")),  # commercial fact
        lambda: evaluate(quotation(), cust=customer(status="credit_restricted")),
        lambda: evaluate(quotation(requested=date(2026, 12, 31))),  # delivery input
        lambda: evaluate(quotation(snapshots=SnapshotSelection(INV, CAP))),  # snapshot pins
        lambda: evaluate(quotation(), approval=replace(APPROVAL, fingerprint="b" * 64)),  # rule
        lambda: evaluate(quotation((FIRST, SECOND))),  # lines
        lambda: evaluate(quotation((SECOND, FIRST))),  # line order
        lambda: evaluate(quotation((replace(FIRST, tolerance_mm=D("0.4")),))),  # part field
    ],
)
def test_relevant_changes_change_the_fingerprint(change):
    assert change().evaluation_fingerprint != evaluate(quotation()).evaluation_fingerprint


def test_snapshot_selection_changes_fingerprint_not_calculation_id():
    pinned_a = evaluate(quotation(snapshots=SnapshotSelection(INV, CAP)))
    other = SnapshotSelection(replace(INV, snapshot_id="INV-B"), replace(CAP, snapshot_id="CAP-B"))
    pinned_b = evaluate(quotation(snapshots=other))
    assert pinned_a.quotation.quote_total_usd == pinned_b.quotation.quote_total_usd
    assert pinned_a.quotation.calculation_id == pinned_b.quotation.calculation_id
    assert pinned_a.evaluation_fingerprint != pinned_b.evaluation_fingerprint


def test_fingerprint_ignores_the_callers_decimal_context():
    expected = evaluate(quotation()).evaluation_fingerprint
    with localcontext() as context:
        context.prec, context.rounding = 6, ROUND_FLOOR
        assert evaluate(quotation()).evaluation_fingerprint == expected


def test_contradiction_evaluations_are_fingerprinted_from_their_parts():
    one = evaluate_outcome(customer(), None, APPROVAL, ("QUANTITY_CONFLICT",), parts=[FIRST])
    two = evaluate_outcome(customer(), None, APPROVAL, ("QUANTITY_CONFLICT",), parts=[SECOND])
    assert one.parts == (FIRST,) and one.evaluation_fingerprint != two.evaluation_fingerprint


def test_customer_contact_details_are_not_part_of_the_evaluation():
    named = Customer(
        customer_id="CUST-T",
        company_name="Private Name Ltd",
        account_status="active",
        credit_limit_usd=D("25000"),
        credit_exposure_usd=D("0"),
    )
    e = evaluate(quotation(), cust=named)
    assert e.evaluation_fingerprint == evaluate(quotation()).evaluation_fingerprint
    assert "Private Name" not in canonical_json(e.customer)


def test_canonical_json_refuses_orm_rows():
    with pytest.raises(ValueError):
        canonical_json(customer())


# --- Finding 3: the trusted manifest is re-derivable from the frozen sources ---------


@pytest.mark.parametrize("kind,path,version", RULE_SOURCES)
def test_manifest_matches_the_frozen_rule_sources(kind, path, version):
    raw = (SOURCE / path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    rule_version = version or f"{kind}@sha256:{digest}"
    document = json.loads(raw, parse_float=Decimal)
    assert TRUSTED_RULES[rule_version] == (kind, digest, fingerprint(document))


def test_manifest_registers_the_seeded_rules_and_approved_additions_only():
    baseline = {version or TRUSTED_KEY[kind] for kind, _, version in RULE_SOURCES}
    assert len(baseline) == 9 and set(TRUSTED_RULES) == baseline | set(REGISTERED)
    assert set(REGISTERED) == {"approval-v1.2"}
    with pytest.raises(TypeError):
        TRUSTED_RULES["price-v9.9"] = ("pricing", "0" * 64, "0" * 64)


# --- Finding 1: one snapshot per calculation, no mixing -------------------------------


def test_stock_rows_of_two_snapshots_are_never_summed():
    two = stock("1000", ("INV-1", "SNAP-A", 7, "600"), ("INV-2", "SNAP-B", 7, "400"))
    with pytest.raises(SnapshotMismatchError):
        decide([FIRST], stock_map={(FIRST.material_id, FIRST.thickness_mm): two})


def test_stock_rows_must_belong_to_the_selected_snapshot():
    parts = [FIRST]
    checks = [run(p, routing=route_for(p)) for p in parts]
    rows = {(FIRST.material_id, FIRST.thickness_mm): stock("1000")}  # SNAP-1
    with pytest.raises(SnapshotMismatchError):
        evaluate_inventory(parts, checks, rows, PRICING, snapshot_id="SNAP-2")
    ok = evaluate_inventory(parts, checks, rows, PRICING, snapshot_id="SNAP-1")
    assert ok.snapshot_id == "SNAP-1" and ok.lines[0].snapshot_ids == ("SNAP-1",)


def snapshot_reading(workcentre_id, rows):
    """A capacity reading whose rows are (week index, free hours, snapshot id)."""
    weeks = tuple(
        ProductionCapacity(
            workcentre_id=workcentre_id,
            week_start=MONDAY + timedelta(weeks=w),
            remaining_hours=D(h),
            snapshot_id=snapshot,
        )
        for w, h, snapshot in rows
    )
    return replace(reading(workcentre_id, {}), weeks=weeks)


def test_two_capacity_rows_for_one_week_are_rejected():
    doubled = snapshot_reading("WC-LAS", [(0, "500", "SNAP-CAP"), (0, "500", "SNAP-OTHER")])
    with pytest.raises(SnapshotMismatchError, match="two rows"):
        plan(capacity=plenty(**{"WC-LAS": doubled}))


def test_capacity_weeks_of_two_snapshots_are_rejected():
    mixed = snapshot_reading("WC-LAS", [(0, "500", "SNAP-OTHER"), (1, "500", "SNAP-OTHER")])
    with pytest.raises(SnapshotMismatchError, match="more than the selected"):
        plan(capacity=plenty(**{"WC-LAS": mixed}))


def test_capacity_snapshot_pin_is_checked_and_recorded():
    with pytest.raises(SnapshotMismatchError):
        plan_pinned("SNAP-OTHER")
    check = plan_pinned("SNAP-CAP")
    assert (check.result, check.snapshot_id, check.capacity_snapshot_ids) == (
        "feasible",
        "SNAP-CAP",
        ("SNAP-CAP",),
    )


def plan_pinned(snapshot_id):
    parts = [BRACKET]
    return evaluate_capacity(
        parts,
        decide(parts),
        ROUTING,
        plenty(),
        manufacturing_rules=RULES,
        pricing_rules=PRICING,
        horizon_start=MONDAY,
        snapshot_id=snapshot_id,
    )


# --- Finding 8: selection consistency (dates: test_hardening_public.py) ----------------


def test_pins_contradicting_the_selection_are_rejected():
    inventory = decide([FIRST])
    capacity = plan([FIRST], inventory=inventory)
    with pytest.raises(SnapshotMismatchError, match="pins differ"):
        evaluate_lead_time(
            inventory,
            capacity,
            horizon_start=MONDAY,
            inventory_snapshot="INV-OTHER",
            snapshots=SnapshotSelection(INV, CAP),
        )


def test_checks_made_from_another_snapshot_are_rejected():
    inventory = replace(decide([FIRST]), snapshot_id="INV-OTHER")
    capacity = plan([FIRST], inventory=inventory)
    with pytest.raises(SnapshotMismatchError, match="other snapshots"):
        evaluate_lead_time(
            inventory, capacity, horizon_start=MONDAY, snapshots=SnapshotSelection(INV, CAP)
        )


# --- TD-004: borderline_bend_radius under approval-v1.2 -------------------------------

V12_ROW = registered_rule_row("approval-v1.2")
APPROVAL_V12 = replace(
    APPROVAL,
    rule_version="approval-v1.2",
    sha256=V12_ROW["sha256"],
    fingerprint=fingerprint(V12_ROW["document"]),
    document=V12_ROW["document"],
)


def test_approval_v1_2_is_v1_1_plus_only_the_approved_threshold():
    added = {"engineering_review_thresholds", "supersedes", "change_reference"}
    v11, v12 = APPROVAL.document, APPROVAL_V12.document
    assert set(v12) - set(v11) == added
    assert {k: v for k, v in v12.items() if k not in added | {"version"}} == {
        k: v for k, v in v11.items() if k != "version"
    }
    setting = v12["engineering_review_thresholds"]["borderline_bend_radius"]
    assert setting["radius_below_minimum_ratio"] == "1.25"
    assert TRUSTED_RULES["approval-v1.2"] == (
        "approval",
        V12_ROW["sha256"],
        APPROVAL_V12.fingerprint,
    )


def bent(radius):
    return quote((replace(FIRST, bend_radius_mm=radius),))


def minimum():
    return quote((FIRST,)).lines[0].manufacturing.min_bend_radius_mm


def test_minimum_bend_radius_is_recorded_for_bent_lines_only():
    assert minimum() is not None and minimum() > 0
    assert quote((FLAT,)).lines[0].manufacturing.min_bend_radius_mm is None


@pytest.mark.parametrize(
    "scale,borderline",
    [
        (D("1"), True),  # exactly the minimum
        (D("1.249"), True),  # just below 1.25 x minimum
        (D("1.25"), False),  # boundary: 1.25 x minimum is not borderline
        (D("2"), False),
    ],
)
def test_borderline_boundaries(scale, borderline):
    radius = (minimum() * scale).quantize(D("0.001"))
    e = evaluate(bent(radius), approval=APPROVAL_V12)
    assert e.unevaluated_review_conditions == ()
    if borderline:
        assert (e.outcome, e.escalation_reason, e.borderline_bend_lines) == (
            "engineering_review_required",
            "BORDERLINE_BEND_RADIUS",
            (1,),
        )
        assert e.draft_creation_allowed is True  # review is separate from drafting
    else:
        assert (e.outcome, e.borderline_bend_lines) == ("quote_draft_ready_for_approval", ())


def test_radius_below_minimum_is_still_declined_not_borderline():
    e = evaluate(bent(minimum() - D("0.01")), approval=APPROVAL_V12)
    assert (e.outcome, e.borderline_bend_lines) == ("unsupported_declined", ())


def test_unstated_radius_and_flat_parts_are_not_borderline():
    unstated = evaluate(bent(None), approval=APPROVAL_V12)
    flat = evaluate(quote((FLAT,)), approval=APPROVAL_V12)
    assert unstated.borderline_bend_lines == flat.borderline_bend_lines == ()
    assert unstated.outcome == flat.outcome == "quote_draft_ready_for_approval"


def test_only_the_borderline_line_is_named():
    second = replace(SECOND, bend_radius_mm=minimum())  # borderline
    e = evaluate(quote((FIRST, second)), approval=APPROVAL_V12)
    assert e.borderline_bend_lines == (2,)


def test_borderline_keeps_the_existing_precedence():
    radius = minimum()
    hold = evaluate(bent(radius), customer("on_hold"), approval=APPROVAL_V12)
    assert hold.outcome == "customer_hold_blocked_release"
    breach = evaluate(bent(radius), customer(exposure="24990"), approval=APPROVAL_V12)
    assert (breach.outcome, breach.escalation_reason) == (
        "engineering_review_required",
        "CREDIT_LIMIT_EXCEEDED",  # BORDERLINE_BEND_RADIUS ranks last as a reason
    )


def test_v1_1_still_reports_the_condition_unevaluated():
    e = evaluate(bent(minimum()))
    assert (e.unevaluated_review_conditions, e.borderline_bend_lines) == (
        ("borderline_bend_radius",),
        (),
    )
    assert e.outcome == "quote_draft_ready_for_approval"


@pytest.mark.parametrize(
    "thresholds,message",
    [
        ({"borderline_bend_radius": {"radius_below_minimum_ratio": "1"}}, "greater than 1"),
        ({"borderline_bend_radius": {"radius_below_minimum_ratio": "0.8"}}, "greater than 1"),
        (
            {"borderline_bend_radius": {"radius_below_minimum_ratio": 1.25}},
            "borderline_bend_radius",
        ),  # float
        ({"borderline_bend_radius": {}}, "borderline_bend_radius"),
        ({"thin_wall": {"ratio": "2"}}, "unknown conditions"),
        ("1.25", "unknown conditions"),
    ],
)
def test_malformed_thresholds_fail_closed(thresholds, message):
    document = {**APPROVAL_V12.document, "engineering_review_thresholds": thresholds}
    with pytest.raises(ValueError, match=message):
        evaluate(bent(minimum()), approval=replace(APPROVAL_V12, document=document))


def test_threshold_for_an_undeclared_condition_is_contradictory():
    document = {
        **APPROVAL_V12.document,
        "engineering_review_conditions": ["unsupported_or_unknown_geometry"],
    }
    with pytest.raises(ValueError, match="does not declare"):
        evaluate(approval=replace(APPROVAL_V12, document=document))
