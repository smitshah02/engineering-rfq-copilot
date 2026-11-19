"""K3 (integrity) offline checks: validity, fingerprints, rule file, allowlist, receipts.

No database: trigger and constraint behaviour is in
``test_k3_calculation_provenance_database.py``. Some tests here fail on purpose until the
user pins measured values (the K3 guard-file SHA-256 and the workflow-v1.2 manifest entry):
their failure message prints the value to pin.
"""

import hashlib
import importlib.util
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_quotation_database import RFQ_0041
from test_quote_drafts import draft as draft_receipt
from test_quote_drafts import inputs as draft_inputs

import rfq_copilot.services.pricing as pricing_module
from rfq_copilot import quote_drafts, quote_evidence, quote_versions, schema_contract
from rfq_copilot.api.auth import Principal
from rfq_copilot.canonical import fingerprint
from rfq_copilot.entities import Part
from rfq_copilot.quote_drafts import QuoteDraft
from rfq_copilot.quote_versions import revision_digest
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES
from rfq_copilot.seeding.safety import expected_trigger, trigger_type
from rfq_copilot.services.pricing import calculation_id, calculation_record_content

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "src" / "rfq_copilot" / "rules"
K3_FILE = ROOT / "alembic" / "versions" / "20261116_01_k3_calculation_provenance.py"
D = Decimal


def k3_module():
    """The K3 revision read directly (no pin check), for structural assertions."""
    spec = importlib.util.spec_from_file_location("k3_revision_offline", K3_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow(document, version="workflow-v1.2"):
    return SimpleNamespace(rule_version=version, document=document)


V12 = json.loads((RULES / "workflow-v1.2.json").read_text(), parse_float=Decimal)
V11 = json.loads((RULES / "workflow-v1.1.json").read_text(), parse_float=Decimal)


# --- Validity: 30 UTC calendar dates inclusive, from the pinned rule -------------------------


@pytest.mark.parametrize(
    "created,last_valid",
    [(datetime(2026, 11, 16, 0, 0, tzinfo=UTC), date(2026, 12, 15)),
     (datetime(2026, 11, 16, 23, 59, 59, 999999, tzinfo=UTC), date(2026, 12, 15)),
     (datetime(2026, 11, 17, 0, 0, tzinfo=UTC), date(2026, 12, 16)),
     (datetime(2026, 12, 15, 12, 0, tzinfo=UTC), date(2027, 1, 13)),
     (datetime(2027, 1, 31, 8, 0, tzinfo=UTC), date(2027, 3, 1)),
     (datetime(2028, 1, 31, 8, 0, tzinfo=UTC), date(2028, 2, 29)),
     # 20:00 at UTC-5 is 01:00 UTC the next day: the UTC date is day one.
     (datetime(2026, 11, 16, 20, 0, tzinfo=timezone(timedelta(hours=-5))), date(2026, 12, 16))],
)  # fmt: skip
def test_expiry_is_the_29th_day_after_the_utc_creation_date(created, last_valid):
    dates = quote_evidence.validity_dates(workflow(V12))
    assert dates == 30  # from the rule document, not a service constant
    assert quote_evidence.expiry_date(created, dates) == last_valid
    assert quote_evidence.expiry_date(created, dates) != created.astimezone(UTC).date() + timedelta(days=30)  # fmt: skip
    expires = quote_evidence.expires_at(last_valid)
    assert expires == datetime.combine(last_valid + timedelta(days=1), datetime.min.time(), tzinfo=UTC)  # fmt: skip
    assert not quote_evidence.is_expired(last_valid, expires - timedelta(microseconds=1))
    assert quote_evidence.is_expired(last_valid, expires)


def test_naive_times_and_invalid_counts_are_refused():
    with pytest.raises(ValueError):
        quote_evidence.expiry_date(datetime(2026, 11, 16), 30)
    with pytest.raises(ValueError):
        quote_evidence.is_expired(date(2026, 12, 15), datetime(2026, 12, 16))
    for dates in (0, -1, 30.0, True, "30"):
        with pytest.raises(ValueError):
            quote_evidence.expiry_date(datetime(2026, 11, 16, tzinfo=UTC), dates)


@pytest.mark.parametrize(
    "setting",
    [{"calendar_dates_inclusive": 0, "first_date": "version_created_at_utc_date"},
     {"calendar_dates_inclusive": "30", "first_date": "version_created_at_utc_date"},
     {"calendar_dates_inclusive": True, "first_date": "version_created_at_utc_date"},
     {"calendar_dates_inclusive": 30, "first_date": "submitted_date"},
     {"calendar_dates_inclusive": 30}, [30], "30"],
)  # fmt: skip
def test_a_malformed_validity_rule_fails_closed(setting):
    with pytest.raises(ValueError):
        quote_evidence.validity_dates(workflow({**V12, "quote_validity": setting}))


def test_legacy_workflow_rules_state_no_validity():
    assert quote_evidence.validity_dates(workflow(V11, "workflow-v1.1")) is None
    assert quote_drafts.expected_expiry(workflow(V11, "workflow-v1.1"), datetime.now(UTC)) is None


# --- workflow-v1.2 rule file and manifest ----------------------------------------------------


def test_workflow_v1_2_is_v1_1_plus_only_quote_validity():
    meta = {"version", "supersedes", "change_reference", "quote_validity"}
    assert {k: v for k, v in V12.items() if k not in meta} == {
        k: v for k, v in V11.items() if k not in {"version", "supersedes", "change_reference"}
    }
    assert (V12["version"], V12["supersedes"]) == ("workflow-v1.2", "workflow-v1.1")
    assert V12["quote_validity"] == {"calendar_dates_inclusive": 30, "first_date": "version_created_at_utc_date"}  # fmt: skip
    assert "quote_validity" not in V11  # the historical rule is untouched
    assert quote_drafts.WORKFLOW_RULE_VERSION == "workflow-v1.2"


def test_workflow_v1_2_manifest_entry_is_pinned_to_the_file():
    """Fails until the user pins the measured values; the message prints them."""
    raw = (RULES / "workflow-v1.2.json").read_bytes()
    measured = ("workflow", hashlib.sha256(raw).hexdigest(), fingerprint(json.loads(raw, parse_float=Decimal)))  # fmt: skip
    assert TRUSTED_RULES["workflow-v1.2"] == measured, f"pin rule_manifest workflow-v1.2 = {measured}"  # fmt: skip


# --- Calculation identity and content --------------------------------------------------------


def test_calculation_id_is_unchanged():
    parts = [Part.from_mapping(p) for p in RFQ_0041["expected_extraction"]]
    assert calculation_id(parts, "price-v1.0") == RFQ_0041["expected_pricing"]["calculation_id"]


PRICE_FP, MFG_FP = "p" * 64, "m" * 64
PART = Part(part_id="P1", material_id="M1", thickness_mm=D("2.0"), quantity=10)
UNROUNDED = pricing_module._UNROUNDED


def fake_calculation(**changes):
    values = {"cost_per_kg": D("2.5"), "density": D("7850"), "margin": D("0.25"), "minutes": D("1.5"),
              "rate": D("60"), "mfg_fp": MFG_FP, "price_fp": PRICE_FP, "total": D("100.00"),
              "scrap": D("0.05"), "unrounded": D("1"), **changes}  # fmt: skip
    op = SimpleNamespace(operation_id="laser", run_minutes_per_unit=values["minutes"],
                         hourly_rate_usd=values["rate"], labour_per_unit_usd=D("1.5"),
                         setup_minutes=D("10"), setup_cost_usd=D("10"))  # fmt: skip
    cost = SimpleNamespace(part_id="P1", material_cost_usd_per_kg=values["cost_per_kg"], operation_costs=(op,),
                           **dict.fromkeys(UNROUNDED, values["unrounded"]))  # fmt: skip
    material = SimpleNamespace(material_id="M1", density_kg_m3=values["density"])
    check = SimpleNamespace(rule_version="mfg-v1.0", rule_fingerprint=values["mfg_fp"], material=SimpleNamespace(material=material))  # fmt: skip
    inventory = SimpleNamespace(manufacturing=(check,), scrap_fraction=values["scrap"])
    return SimpleNamespace(
        result="priced", calculation_id="CALC-X", currency="USD", gross_margin=values["margin"],
        lines=(SimpleNamespace(cost=cost, line_total_usd=values["total"]),),
        quote_total_usd=values["total"], tax_included=False, freight_included=False,
        shipping_included=False, costs=SimpleNamespace(inventory=inventory),
        pricing_rule_version="price-v1.0", pricing_rule_fingerprint=values["price_fp"],
    )  # fmt: skip


@pytest.fixture
def content(monkeypatch):
    monkeypatch.setattr(
        pricing_module, "display_quote",
        lambda calc, rules: {"lines": [{"part_id": line.cost.part_id, "quantity": 10, "line_total_usd": str(line.line_total_usd)} for line in calc.lines]},
    )  # fmt: skip

    def compute(**changes):
        calc = fake_calculation(**changes)
        fp = calc.pricing_rule_fingerprint
        rules = SimpleNamespace(rule_version="price-v1.0", fingerprint=fp)
        return calculation_record_content(calc, rules, [PART])

    return compute


@pytest.mark.parametrize(
    "change",
    [{"cost_per_kg": D("2.6")}, {"density": D("7900")}, {"margin": D("0.30")},
     {"minutes": D("1.6")}, {"rate": D("61")}, {"mfg_fp": "n" * 64}, {"price_fp": "q" * 64},
     {"total": D("100.01")}, {"scrap": D("0.06")}, {"unrounded": D("1.0000001")}],
)  # fmt: skip
def test_every_price_affecting_input_changes_the_content_fingerprint(content, change):
    lines, digest, manufacturing = content()
    assert content() == (lines, digest, manufacturing)  # deterministic
    assert content(**change)[1] != digest


def test_content_is_rfq_customer_and_snapshot_free_and_records_the_raw_inputs(content):
    lines, _, manufacturing = content()
    text = json.dumps(lines)
    for absent in ("rfq", "customer", "snapshot", "horizon", "expiry"):
        assert absent not in text
    (line,) = lines
    assert line["material"] == {"material_id": "M1", "cost_usd_per_kg": "2.5", "density_kg_m3": "7850"}  # fmt: skip
    assert line["part"]["part_id"] == "P1" and line["operation_costs"][0]["operation_id"] == "laser"
    assert manufacturing == "mfg-v1.0"


def test_inconsistent_calculations_have_no_content(content, monkeypatch):
    calc = fake_calculation()
    with pytest.raises(ValueError, match="different pricing rule"):
        calculation_record_content(calc, SimpleNamespace(rule_version="price-v1.0", fingerprint="x" * 64), [PART])  # fmt: skip
    two = replace_inventory(calc, extra_check=True)
    with pytest.raises(ValueError, match="exactly one manufacturing rule"):
        calculation_record_content(two, SimpleNamespace(rule_version="price-v1.0", fingerprint=PRICE_FP), [PART])  # fmt: skip
    with pytest.raises(ValueError, match="Only a priced"):
        calculation_record_content(SimpleNamespace(result="needs_clarification", quote_total_usd=None), None, [PART])  # fmt: skip


def replace_inventory(calc, extra_check):
    check = calc.costs.inventory.manufacturing[0]
    other = SimpleNamespace(**{**vars(check), "rule_fingerprint": "z" * 64})
    inventory = SimpleNamespace(manufacturing=(check, other), scrap_fraction=D("0.05"))
    return SimpleNamespace(**{**vars(calc), "costs": SimpleNamespace(inventory=inventory)})


# --- Version evidence digests ---------------------------------------------------------------


def line(number=10, revision=1, **part):
    fields = {"part_id": "P1", "material_id": "M1", "thickness_mm": D("2.0"), "quantity": 10, **part}  # fmt: skip
    values = {f: fields.get(f) for f in Part.__dataclass_fields__}
    return SimpleNamespace(line_number=number, requirement_revision=revision, **values)


def test_rfq_source_digest_binds_rfq_customer_revision_and_specification():
    base = quote_evidence.rfq_source_sha256("RFQ-1", "CUST-1", [line(10), line(20, part_id="P2")])
    assert base == quote_evidence.rfq_source_sha256("RFQ-1", "CUST-1", [line(20, part_id="P2"), line(10)])  # fmt: skip
    for variant in (
        quote_evidence.rfq_source_sha256("RFQ-2", "CUST-1", [line(10), line(20, part_id="P2")]),
        quote_evidence.rfq_source_sha256("RFQ-1", "CUST-2", [line(10), line(20, part_id="P2")]),
        quote_evidence.rfq_source_sha256("RFQ-1", "CUST-1", [line(10, revision=2), line(20, part_id="P2")]),
        quote_evidence.rfq_source_sha256("RFQ-1", "CUST-1", [line(10, quantity=11), line(20, part_id="P2")]),
    ):  # fmt: skip
        assert variant != base


def test_rule_pin_digest_changes_with_any_pinned_fingerprint():
    def rule(version, kind, fp):
        return SimpleNamespace(rule_version=version, rule_kind=kind, fingerprint=fp)

    rules = {"manufacturing_rules": rule("mfg-v1.0", "manufacturing", "1" * 64),
             "pricing_rules": rule("price-v1.0", "pricing", "2" * 64),
             "approval_rules": rule("approval-v1.2", "approval", "3" * 64)}  # fmt: skip
    base = quote_evidence.rules_sha256(rules, rule("workflow-v1.2", "workflow", "4" * 64))
    assert base != quote_evidence.rules_sha256(rules, rule("workflow-v1.2", "workflow", "5" * 64))
    changed = {**rules, "pricing_rules": rule("price-v1.0", "pricing", "6" * 64)}
    assert base != quote_evidence.rules_sha256(changed, rule("workflow-v1.2", "workflow", "4" * 64))


# --- Receipts: customer-safe, replayable across the K3 change --------------------------------


def test_receipts_carry_validity_but_no_record_identity_or_cost():
    receipt = replace(draft_receipt(), expiry_date=date(2026, 12, 15))
    data = receipt.to_json()
    assert data["expiry_date"] == "2026-12-15"
    assert QuoteDraft.from_json(data) == replace(receipt, replayed=True)
    legacy = {k: v for k, v in data.items() if k != "expiry_date"}  # written before K3
    assert QuoteDraft.from_json(legacy).expiry_date is None
    assert "calculation_record_id" not in QuoteDraft.__dataclass_fields__
    assert not re.search(r"cost|margin|density|labor", repr(data))


def test_adoption_is_explicit_one_way_and_keeps_ordinary_digests():
    principal = Principal("CUST-001", frozenset(), user_id="USER-001")
    ordinary = revision_digest(principal, "QUOTE-1", 1, draft_inputs())
    assert ordinary == revision_digest(principal, "QUOTE-1", 1, draft_inputs(), None)
    assert ordinary != revision_digest(principal, "QUOTE-1", 1, draft_inputs(), "workflow-v1.2")
    assert quote_versions.ADOPTABLE_FROM == {"workflow-v1.1"}  # never v1.0
    assert quote_versions.ADOPT_OPERATION == "quote_version.adopt_workflow"


def test_quote_services_never_read_the_application_clock():
    for module in (quote_drafts, quote_versions, quote_evidence):
        assert "datetime.now" not in Path(module.__file__).read_text(), module.__name__


# --- K3 guard allowlist ----------------------------------------------------------------------


def test_k3_revision_defines_its_own_guards_and_reuses_k2_refusal():
    module = k3_module()
    assert (module.revision, module.down_revision) == ("20261116_01", "20261115_01")
    assert set(module.FUNCTIONS) == {"k3_quote_versions_evidence", "k3_complete_version_lines", "k3_pricing_calculations_seal"}  # fmt: skip
    by_name = {t[0]: t for t in module.TRIGGERS}
    assert by_name["k3_quote_version_lines_complete"][1:] == ("quote_versions", "k3_complete_version_lines", ("INSERT",), "deferred")  # fmt: skip
    assert by_name["k3_quote_line_items_complete"][1:] == ("quote_line_items", "k3_complete_version_lines", ("INSERT",), "deferred")  # fmt: skip
    assert by_name["k3_business_rule_versions_immutable"][1:] == ("business_rule_versions", "k2_refuse_change", ("UPDATE", "DELETE"), "before")  # fmt: skip
    for body in module.FUNCTIONS.values():
        assert "CONSTRAINT = TG_NAME" in body and "integrity_constraint_violation" in body
        assert "EXECUTE " not in body and "current_setting" not in body
    assert "transaction_timestamp()" in module.FUNCTIONS["k3_quote_versions_evidence"]
    assert "transaction_timestamp()" in module.FUNCTIONS["k3_pricing_calculations_seal"]
    # No owner exemption on calculation records: seeding never writes them.
    assert "pg_has_role" not in module.FUNCTIONS["k3_pricing_calculations_seal"]
    assert "quote_validity" in module.FUNCTIONS["k3_quote_versions_evidence"]
    source = K3_FILE.read_text()
    assert "DISABLE TRIGGER" not in source.upper() and "SECURITY DEFINER" not in source
    assert "UPDATE business." not in source  # no backfill of any existing row


def test_k3_guard_file_is_pinned_independently():
    """Fails until the user pins ``shasum -a 256`` of the K3 file; K2's pin is preserved."""
    measured = hashlib.sha256(K3_FILE.read_bytes()).hexdigest()
    assert schema_contract.GUARD_FILE_SHA256["20261116_01"] == measured, f"pin {measured}"
    assert schema_contract.GUARD_FILE_SHA256["20261115_01"] == (
        "10021900678979e4ce1c1d48c64fbcafa8251fc724c11a96659c702ab5bf082d"
    )


def test_merged_allowlist_rejects_duplicates_unknown_kinds_and_missing_functions(monkeypatch):
    k2 = ({"k2_refuse_change": "x"}, (("k2_t", "rfqs", "k2_refuse_change", ("UPDATE",)),))
    cases = [
        ({"k2_refuse_change": "y"}, ()),  # function defined twice
        ({}, (("k2_t", "rfqs", "k2_refuse_change", ("UPDATE",), "before"),)),  # duplicate trigger
        ({}, (("k3_t", "rfqs", "k2_refuse_change", ("UPDATE",), "after"),)),  # unknown kind
        ({}, (("k3_t", "rfqs", "k3_missing", ("UPDATE",), "before"),)),  # no such function
    ]
    contract = SimpleNamespace(guards_revisions=("a", "b"))
    monkeypatch.setattr(schema_contract, "dropped_triggers", lambda revision: ())
    for second in cases:
        monkeypatch.setattr(schema_contract, "history_guards", {"a": k2, "b": second}.__getitem__)
        with pytest.raises(ValueError):
            schema_contract.contract_guards(contract)
    monkeypatch.setattr(schema_contract, "history_guards", {"a": k2, "b": ({}, ())}.__getitem__)
    _, triggers = schema_contract.contract_guards(contract)
    assert triggers == (("k2_t", "rfqs", "k2_refuse_change", ("UPDATE",), "before"),)
    # K4: a later revision may drop an earlier trigger, never one that was not created.
    k4 = ({"k4_f": "z"}, (("k4_t", "rfqs", "k4_f", ("UPDATE",)),))
    monkeypatch.setattr(schema_contract, "history_guards", {"a": k2, "b": k4}.__getitem__)
    monkeypatch.setattr(schema_contract, "dropped_triggers", {"a": (), "b": ("k2_t",)}.__getitem__)
    functions, triggers = schema_contract.contract_guards(contract)
    assert set(functions) == {"k2_refuse_change", "k4_f"}  # the replaced function stays defined
    assert triggers == (("k4_t", "rfqs", "k4_f", ("UPDATE",), "before"),)
    monkeypatch.setattr(schema_contract, "dropped_triggers", {"a": ("k2_t",), "b": ()}.__getitem__)
    with pytest.raises(ValueError, match="dropped before"):
        schema_contract.contract_guards(contract)


def test_expected_catalog_facts_of_before_and_deferred_triggers():
    assert expected_trigger("rfqs", "f", ("UPDATE",), "before") == (
        "business", "rfqs", "f", trigger_type(("UPDATE",)), "A", 0, True, "", True, False, False)  # fmt: skip
    assert expected_trigger("quote_versions", "f", ("INSERT",), "deferred") == (
        "business", "quote_versions", "f", 1 | 4, "A", 0, True, "", False, True, True)  # fmt: skip


def test_new_contracts_are_either_unmeasured_or_pinned_measurements():
    for contract in (schema_contract.CURRENT, schema_contract.NEXT):
        assert contract.shape_digest == schema_contract.UNMEASURED or re.fullmatch(
            "[0-9a-f]{64}", contract.shape_digest
        )
        counts = dict(contract.catalog_counts)
        assert counts == {"unmeasured": -1} or set(counts) == {"p", "f", "u", "c"}


# --- Downgrade symmetry (the initial revision's frozen downgrade shape) ------------------


def _pricing_table(path: Path, function: str | None = None) -> tuple[list, list]:
    """(column names in order, constraint names) of the pricing_calculations create_table."""
    import ast

    tree = ast.parse(path.read_text())
    scope = tree if function is None else next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function
    )  # fmt: skip
    (call,) = [
        node for node in ast.walk(scope)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "create_table"
        and isinstance(node.args[0], ast.Constant) and node.args[0].value == "pricing_calculations"
    ]  # fmt: skip
    columns = [a.args[0].value for a in call.args[1:] if getattr(a.func, "attr", "") == "Column"]
    names = sorted(
        kw.value.args[0].value for a in call.args[1:] for kw in a.keywords if kw.arg == "name"
    )
    return columns, names


def test_k3_downgrade_rebuilds_pricing_calculations_in_its_original_physical_form():
    """Regression: ADD COLUMN appended expires_at last, so the initial revision's frozen
    shape check refused the final downgrade to base and left the test DB populated."""
    initial = ROOT / "alembic" / "versions" / "20261108_01_initial_rfq_database_schema.py"
    original = _pricing_table(initial)
    assert original[0][4] == "expires_at"  # position matters for owned_shape (attnum order)
    assert _pricing_table(K3_FILE, "_create_pricing_calculations_before_k3") == original
    module = k3_module()
    assert "ADD COLUMN expires_at" not in module.DOWNGRADE_DDL  # never appended again
    assert "k2_pricing_calculations_immutable" in module.DOWNGRADE_RESTORE_DDL
    assert "ENABLE ALWAYS TRIGGER k2_pricing_calculations_immutable" in module.DOWNGRADE_RESTORE_DDL
    assert "fk_quote_versions_calculation_id_pricing_calculations" in module.DOWNGRADE_RESTORE_DDL
