"""versioning quotation versioning: offline checks (no database, network or model)."""

import csv
import json
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint
from test_quote_drafts import AGENT_MODULES, INITIAL, SRC, called_names, imported_modules
from test_quote_drafts import draft as draft_receipt
from test_quote_drafts import inputs as draft_inputs

import rfq_copilot.quote_drafts as quote_drafts
import rfq_copilot.quote_versions as quote_versions
import rfq_copilot.seeding.rules as seeding_rules
from rfq_copilot.api.auth import Principal
from rfq_copilot.canonical import fingerprint
from rfq_copilot.models import IdempotencyRecord
from rfq_copilot.quote_drafts import DraftRefused, request_digest
from rfq_copilot.quote_versions import QuoteRevision, revise_quote, revision_digest
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.rules import REGISTERED, registered_rule_row

DATASET = SRC.parents[1] / "data" / "rfq_synthetic_data"
MONDAY = date(2026, 11, 9)
PRINCIPAL = Principal("CUST-001", frozenset(), user_id="USER-001")


def revision(**changes):
    base = draft_receipt(status="revised_draft", version=2)
    return replace(QuoteRevision(**vars(base), previous_version=1), **changes)


# --- Request identity -----------------------------------------------------------------------


def test_revision_digest_binds_quote_expected_version_tenant_and_inputs():
    base = revision_digest(PRINCIPAL, "QUOTE-1", 1, draft_inputs())
    assert base == revision_digest(PRINCIPAL, "QUOTE-1", 1, draft_inputs())
    variants = [
        revision_digest(Principal("CUST-002", user_id="USER-001"), "QUOTE-1", 1, draft_inputs()),
        revision_digest(PRINCIPAL, "QUOTE-2", 1, draft_inputs()),
        revision_digest(PRINCIPAL, "QUOTE-1", 2, draft_inputs()),
        revision_digest(PRINCIPAL, "QUOTE-1", 1, draft_inputs(requested_delivery_date=MONDAY)),
        revision_digest(PRINCIPAL, "QUOTE-1", 1, draft_inputs(capacity_snapshot="SNAP")),
        request_digest(PRINCIPAL, "QUOTE-1", draft_inputs()),  # never a draft persistence digest
    ]
    assert base not in variants and len(set(variants)) == len(variants)


def test_revision_receipt_round_trips_and_stays_non_binding():
    original = revision()
    stored = original.to_json()
    assert stored["previous_version"] == 1 and stored["status"] == "revised_draft"
    assert "replayed" not in stored and stored["total_usd"] == "998.98"
    again = QuoteRevision.from_json(stored)
    assert again == replace(original, replayed=True)
    assert (again.human_approval_required, again.approved, again.released) == (True, False, False)
    with pytest.raises(FrozenInstanceError):
        again.approved = True  # type: ignore[misc]
    assert not {k for k in stored if "cost" in k or "margin" in k or "breakdown" in k}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [{"expected_version": 0}, {"expected_version": True}, {"expected_version": "1"},
     {"contradictions": "conflict"}, {"idempotency_key": ""}, {"correlation_id": " "}],
)  # fmt: skip
async def test_invalid_revision_requests_are_refused_without_a_session(changes):
    kwargs = {"expected_version": 1, "idempotency_key": "k", "correlation_id": "c",
              "horizon_start": MONDAY, **changes}  # fmt: skip
    with pytest.raises(DraftRefused) as refused:
        await revise_quote(None, PRINCIPAL, "QUOTE-1", **kwargs)
    assert refused.value.reason == "invalid_request"


# --- Approved contracts the service relies on (no new policy) --------------------------------


def test_revisions_use_approved_operation_permission_and_transition():
    assert quote_versions.OPERATION == "edit_quote_draft"
    checks = [c for c in IdempotencyRecord.__table__.constraints if isinstance(c, CheckConstraint)]
    assert any("'edit_quote_draft'" in str(c.sqltext) for c in checks)
    with (DATASET / "master_data/roles_permissions.csv").open(newline="") as handle:
        granted = {(r["role"], r["permission"]) for r in csv.DictReader(handle) if r["allowed"] == "True"}  # fmt: skip
    assert {("sales_representative", "edit_quote_draft"), ("estimator", "edit_quote_draft")} <= granted  # fmt: skip
    workflow = json.loads((DATASET / "config/workflow_state_rules.json").read_text())
    assert "revised_draft" in workflow["transitions"]["draft"]
    assert "revised_draft" not in workflow["transitions"]["revised_draft"]  # hence v1.1
    assert workflow["version_bump_requires_approval_reset"] is True  # why approved is refused
    # Pending, approved, released, blocked, clarification and expired: never revised. human decision:
    # rejected -> revised_draft, listed by every workflow rule and allowed by K4.
    assert quote_versions.REVISABLE == {"draft", "revised_draft", "rejected"}
    assert "revised_draft" in workflow["transitions"]["rejected"]


# --- workflow-v1.1 (approved 2026-11-15, versioning Option B) ----------------------------------

V11 = registered_rule_row("workflow-v1.1")  # re-derives sha256 and fingerprint, else SeedError


def test_workflow_v1_1_is_v1_0_plus_only_the_revised_draft_self_transition():
    v10 = json.loads((DATASET / "config/workflow_state_rules.json").read_text())
    v11 = V11["document"]
    meta = {"version", "supersedes", "change_reference", "transitions"}
    assert set(v11) - set(v10) == {"supersedes", "change_reference"}
    assert (v11["version"], v11["supersedes"]) == ("workflow-v1.1", "workflow-v1.0")
    assert {k: v for k, v in v11.items() if k not in meta} == {k: v for k, v in v10.items() if k not in meta}  # fmt: skip
    old = v10["transitions"]
    # Every other state, approval and release included, keeps exactly its v1.0 transitions.
    assert v11["transitions"] == {**old, "revised_draft": ["revised_draft", *old["revised_draft"]]}
    assert TRUSTED_RULES["workflow-v1.1"] == ("workflow", V11["sha256"], fingerprint(v11))
    assert V11["source_path"] == "src/rfq_copilot/rules/workflow-v1.1.json"
    assert REGISTERED["workflow-v1.1"] == ("workflow-v1.1.json", "workflow-v1.0")
    assert quote_drafts.WORKFLOW_RULE_VERSION == "workflow-v1.2"  # K3: new drafts only
    assert "quote_validity" not in v11  # v1.1 is never changed; legacy versions stay partial


def test_a_tampered_or_unapproved_workflow_rule_is_never_registered(tmp_path, monkeypatch):
    original = (seeding_rules.RULES_DIR / "workflow-v1.1.json").read_text()
    tampered = original.replace('"expired": []', '"expired": ["revised_draft"]')
    assert tampered != original
    (tmp_path / "workflow-v1.1.json").write_text(tampered)
    monkeypatch.setattr(seeding_rules, "RULES_DIR", tmp_path)
    with pytest.raises(SeedError, match="does not match its trusted-manifest entry"):
        registered_rule_row("workflow-v1.1")
    with pytest.raises(SeedError, match="not an approved post-baseline"):
        registered_rule_row("workflow-v1.3")


def test_version_constraint_names_match_the_approved_migration():
    migration = INITIAL.read_text()
    assert 'op.f("uq_quote_versions_quote_id_version")' in migration
    assert '"uq_quote_versions_current"' in migration
    # pk_quote_versions: the naming convention's "pk_%(table_name)s".
    unique = ("uq_quote_versions_quote_id_version", "uq_quote_versions_current")
    assert quote_versions.VERSION_CONSTRAINTS == {"pk_quote_versions", *unique}


def test_revision_fields_extend_the_draft_receipt():
    fields = set(QuoteRevision.__dataclass_fields__)
    assert fields == set(type(draft_receipt()).__dataclass_fields__) | {"previous_version"}
    assert revision().total_usd == Decimal("998.98")


# --- Boundaries -----------------------------------------------------------------------------


def test_service_never_commits_prints_logs_or_imports_agents():
    service = Path(quote_versions.__file__)
    assert not called_names(service) & {"commit", "print", "getLogger"}
    modules = imported_modules(service)
    assert not {m for m in modules if m == "logging" or m.startswith("logging.")}
    assert not modules & AGENT_MODULES


@pytest.mark.parametrize(
    "module",
    ["supervisor.py", "graph.py", "agent_integration.py", "recovery.py", "ownership.py",
     "observability.py", "escalation.py", "quotation_agent.py"],
)  # fmt: skip
def test_advisory_modules_cannot_revise_quotations(module):
    assert "rfq_copilot.quote_versions" not in imported_modules(SRC / module)
