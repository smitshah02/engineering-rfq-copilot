"""K4 (Day 54D): the lifecycle-guard revision, its contract pins and the runtime allowlists.

Offline: the revision file is read and imported through the contract's SHA-256 gate; no
database. The physical behaviour of the triggers is covered by
tests/test_quote_approval_database.py (runtime role) and the migration lifecycle tests.
"""

import hashlib
import importlib.util
from pathlib import Path

import pytest

from rfq_copilot import schema_contract
from rfq_copilot.database import DECISION_UPDATABLE, RUNTIME_INSERTABLE, RUNTIME_UPDATABLE

ROOT = Path(__file__).resolve().parents[1]
K4_FILE = ROOT / "alembic" / "versions" / "20261116_03_k4_approval_lifecycle.py"
K4_TRIGGERS = {"k4_quotes_guard", "k4_quote_versions_guard", "k4_approval_requests_guard"}
REPLACED = {"k2_quotes_guard", "k2_quote_versions_guard", "k2_approval_requests_immutable"}
TRANSITIONS = ("pending_approval", "approved", "rejected", "released", "revised_draft")


def k4():
    spec = importlib.util.spec_from_file_location("k4_under_test", K4_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_sits_on_k3_and_is_pinned_independently():
    module = k4()
    assert (module.revision, module.down_revision) == ("20261116_03", "20261116_01")
    measured = hashlib.sha256(K4_FILE.read_bytes()).hexdigest()
    assert schema_contract.GUARD_FILE_SHA256["20261116_03"] == measured, f"pin {measured}"
    assert schema_contract.K4.revision == "20261116_03"  # K5 (Day 55) is the head now
    assert schema_contract.K4.guards_revisions == ("20261115_01", "20261116_01", "20261116_03")  # fmt: skip
    assert schema_contract.CURRENT.guards_revisions[:3] == schema_contract.K4.guards_revisions
    # No table, column, constraint or index changes: K3's measured shape stays the contract.
    assert schema_contract.K4.shape_digest == schema_contract.K3.shape_digest
    assert schema_contract.K4.catalog_counts == schema_contract.K3.catalog_counts
    source = K4_FILE.read_text()
    assert not any(s in source for s in ("create_table", "ADD COLUMN", "ADD CONSTRAINT", "CREATE INDEX", "DROP COLUMN"))  # fmt: skip


def test_the_allowlist_replaces_exactly_three_k2_triggers_and_keeps_fifteen():
    module = k4()
    assert {t[0] for t in module.DROPPED_TRIGGERS} == REPLACED
    assert {t[0] for t in module.TRIGGERS} == set(module.FUNCTIONS) == K4_TRIGGERS
    functions, triggers = schema_contract.contract_guards(schema_contract.K4)
    names = {t[0] for t in triggers}
    assert len(triggers) == 15 and K4_TRIGGERS <= names and not REPLACED & names
    assert set(functions) >= K4_TRIGGERS | {"k2_quotes_guard", "k2_quote_versions_guard"}
    by_table = {(t[1], t[0]) for t in triggers}
    assert {("quotes", "k4_quotes_guard"), ("quote_versions", "k4_quote_versions_guard"),
            ("approval_requests", "k4_approval_requests_guard"),
            ("quote_line_items", "k2_quote_line_items_guard")} <= by_table  # fmt: skip
    assert set(schema_contract.dropped_triggers("20261116_03")) == REPLACED
    assert schema_contract.dropped_triggers("20261115_01") == ()


def test_the_guard_bodies_bind_every_transition_to_a_same_transaction_row():
    module = k4()
    quotes = module.FUNCTIONS["k4_quotes_guard"]
    for status in TRANSITIONS:
        assert f"'{status}'" in quotes
    assert quotes.count("transaction_timestamp()") == 4  # submit, approve, reject, release
    assert "r.resolved_by = NEW.approver_id" in quotes and "r.requested_by = NEW.submitted_by" in quotes  # fmt: skip
    assert "h.action = 'released'" in quotes and "h.quote_version = OLD.approved_version" in quotes  # fmt: skip
    assert "NEW.row_version = OLD.row_version + 1" in quotes
    versions = module.FUNCTIONS["k4_quote_versions_guard"]
    assert "NOT OLD.approval_valid AND NEW.approval_valid" in versions
    assert "r.status = 'approved' AND r.resolved_at = transaction_timestamp()" in versions
    assert "NEW.created_xact := pg_current_xact_id()" in versions  # K2 seal kept
    requests = module.FUNCTIONS["k4_approval_requests_guard"]
    assert "OLD.status = 'pending'" in requests and "'superseded'" in requests
    for body in module.FUNCTIONS.values():
        assert "CONSTRAINT = TG_NAME" in body and "integrity_constraint_violation" in body
        assert "EXECUTE " not in body and "current_setting" not in body
    source = K4_FILE.read_text()
    assert "SECURITY INVOKER" in source and "SECURITY DEFINER" not in source
    assert "ENABLE ALWAYS TRIGGER" in source and "allow_test_downgrade" in source


def test_the_runtime_allowlists_name_the_lifecycle_columns_only():
    assert {"approval_requests", "approval_history"} <= RUNTIME_INSERTABLE  # submission
    assert "business_rule_versions" not in RUNTIME_INSERTABLE and "rfqs" not in RUNTIME_INSERTABLE
    decisions = {
        "quotes.approved_version", "quotes.approver_id", "quotes.approval_timestamp",
        "quotes.released_by", "quotes.released_at", "quote_versions.approval_valid",
        "approval_requests.status", "approval_requests.resolved_by", "approval_requests.resolved_at",
    }  # fmt: skip
    # K5 (Day 55): decisions left the runtime role for the decision role.
    assert not decisions & RUNTIME_UPDATABLE and decisions <= DECISION_UPDATABLE
    assert {"quotes.submitted_by", "quotes.rejection_reason"} <= RUNTIME_UPDATABLE
    assert not {c for c in RUNTIME_UPDATABLE if c.startswith(("quote_line_items", "audit", "pricing", "rfq", "users", "role"))}  # fmt: skip
    assert "quotes.created_by" not in RUNTIME_UPDATABLE and "quotes.rfq_id" not in RUNTIME_UPDATABLE


def test_the_deferred_merge_follows_the_head():
    following = schema_contract.NEXT
    assert following.guards_revisions == schema_contract.CURRENT.guards_revisions
    merge = next(schema_contract.DEFERRED.glob("20261116_02_*.py")).read_text()
    assert '("20261111_05", "20261116_04")' in merge


@pytest.mark.parametrize("name", ["20261115_01", "20261116_01"])  # and K4's, above
def test_earlier_guard_pins_are_untouched(name):
    assert schema_contract.GUARD_FILE_SHA256[name] in (
        "10021900678979e4ce1c1d48c64fbcafa8251fc724c11a96659c702ab5bf082d",
        "f214da512cfc840133719572300a9862a9d0fcba5ae963d61e944bc552df675f",
    )
