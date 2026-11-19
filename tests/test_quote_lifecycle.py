"""human decision offline: release request validation, release blockers, lifecycle contracts and
isolation of the binding paths from agents, the Supervisor and MCP. No database."""

import inspect
import json
from types import SimpleNamespace

import pytest
from test_quote_approval import GOOD, NoDatabase, refused
from test_quote_drafts import AGENT_MODULES, PRINCIPAL, SRC, imported_modules

from rfq_copilot import quote_approval as approval
from rfq_copilot import quote_versions
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.schemas.reads import ReleasedQuote, ReleasedQuoteLine
from rfq_copilot.quote_approval import (
    ReleaseEligibility,
    assess_release,
    decision_digest,
    release_blockers,
    release_quote,
)
from rfq_copilot.services.evaluation import AGENT_MUST_NOT

WORKFLOW = json.loads((SRC / "rules/workflow-v1.2.json").read_text())


def assessment(blockers=("CONTEXT_ATTESTATION_REQUIRED",), classification="policy_conditions_met", reviews=("human_approval",)):  # fmt: skip
    return SimpleNamespace(
        binding_approval_blockers=blockers,
        classification=classification,
        policy=SimpleNamespace(reviews=tuple(SimpleNamespace(kind=k) for k in reviews)),
    )


APPROVAL = SimpleNamespace(approval_request_id="APR-1")


@pytest.mark.parametrize(
    "principal,quote_id,changes",
    [(Principal("CUST-001", frozenset()), "QUOTE-1", {}),
     (PRINCIPAL, "", {}),
     (PRINCIPAL, "QUOTE-1", {"expected_version": 0}),
     (PRINCIPAL, "QUOTE-1", {"expected_version": True}),
     (PRINCIPAL, "QUOTE-1", {"idempotency_key": " k"}),
     (PRINCIPAL, "QUOTE-1", {"correlation_id": ""}),
     ("not a principal", "QUOTE-1", {})],
)  # fmt: skip
def test_invalid_release_requests_are_refused_before_any_database_access(principal, quote_id, changes):  # fmt: skip
    refused("invalid_request", release_quote(NoDatabase(), principal, quote_id, **{**GOOD, **changes}))  # fmt: skip
    if not {"idempotency_key", "correlation_id"} & set(changes):  # the read takes neither
        refused("invalid_request", assess_release(NoDatabase(), principal, quote_id, expected_version=changes.get("expected_version", 1)))  # fmt: skip


def test_an_approved_attested_version_has_no_release_blocker():
    assert release_blockers(assessment(), APPROVAL) == ()
    assert release_blockers(assessment(blockers=()), APPROVAL) == ()


@pytest.mark.parametrize(
    "changes,approval_,expected",
    [({"blockers": ("CONTEXT_ATTESTATION_REQUIRED", "QUOTE_EXPIRED")}, APPROVAL, ("QUOTE_EXPIRED",)),
     ({"blockers": ("EVALUATION_CONTEXT_NOT_AUTHENTICATED",)}, APPROVAL, ("EVALUATION_CONTEXT_NOT_AUTHENTICATED",)),
     ({"blockers": ("CONTEXT_ATTESTATION_REQUIRED", "EVALUATION_CHANGED")}, APPROVAL, ("EVALUATION_CHANGED",)),
     ({"blockers": ("CONTEXT_ATTESTATION_REQUIRED", "EVIDENCE_MISMATCH")}, APPROVAL, ("EVIDENCE_MISMATCH",)),
     ({"classification": "commercially_blocked"}, APPROVAL, ("POLICY_NOT_RELEASABLE",)),
     ({"classification": "unresolved_conditions"}, APPROVAL, ("POLICY_NOT_RELEASABLE",)),
     ({"classification": "reviews_required", "reviews": ("human_approval", "engineering_review")}, APPROVAL, ("ENGINEERING_REVIEW_UNVERIFIED",)),
     ({}, None, ("HUMAN_APPROVAL_NOT_VERIFIED",))],
)  # fmt: skip
def test_release_fails_closed_on_every_missing_authority(changes, approval_, expected):
    assert release_blockers(assessment(**changes), approval_) == expected


def test_a_manager_review_is_the_approvers_own_and_does_not_block_release():
    reviewed = assessment(classification="reviews_required", reviews=("human_approval", "manager_review"))  # fmt: skip
    assert release_blockers(reviewed, APPROVAL) == ()


def test_release_digest_is_its_own_operation():
    release = decision_digest(PRINCIPAL, "QUOTE-1", 1, approval.RELEASE, {})
    assert release == decision_digest(PRINCIPAL, "QUOTE-1", 1, approval.RELEASE, {})
    others = {decision_digest(PRINCIPAL, "QUOTE-1", 1, op, {}) for op in (approval.SUBMIT, approval.APPROVE, approval.REJECT)}  # fmt: skip
    others.add(decision_digest(PRINCIPAL, "QUOTE-1", 2, approval.RELEASE, {}))
    others.add(decision_digest(Principal("CUST-002", frozenset(), user_id="USER-001"), "QUOTE-1", 1, approval.RELEASE, {}))  # fmt: skip
    assert release not in others and len(others) == 5


def test_release_eligibility_is_codes_only():
    fields = set(ReleaseEligibility.__dataclass_fields__)
    assert fields == {"quote_id", "rfq_id", "customer_id", "version", "status", "approval_request_id", "blockers", "assessment_fingerprint"}  # fmt: skip
    eligible = ReleaseEligibility(quote_id="Q", rfq_id="R", customer_id="C", version=1, status="approved", approval_request_id="APR-1", blockers=(), assessment_fingerprint="a" * 64)  # fmt: skip
    assert eligible.eligible and not ReleaseEligibility(**{**eligible.__dict__, "blockers": ("QUOTE_EXPIRED",)}).eligible  # fmt: skip


def test_the_customer_projection_carries_no_internal_financials():
    exposed = set(ReleasedQuote.model_fields) | set(ReleasedQuoteLine.model_fields)
    for word in ("cost", "margin", "labor", "breakdown", "evidence", "calculation", "approver", "fingerprint"):  # fmt: skip
        assert not any(word in field for field in exposed), word


def test_lifecycle_uses_only_approved_transitions_operations_and_grants():
    transitions = WORKFLOW["transitions"]
    assert "released" in transitions["approved"]  # workflow-v1.2
    assert "revised_draft" in transitions["rejected"]
    assert approval.RELEASE == "release_quote" and approval.AUDIT[approval.RELEASE] == "quote_approval.release"  # fmt: skip
    assert "rejected" in quote_versions.REVISABLE
    # K4 has no approved -> expired, approved/pending -> revised_draft branch: never attempted.
    assert not {"approved", "pending_approval"} & quote_versions.REVISABLE


def test_binding_paths_are_human_only_and_unreachable_from_agents():
    assert {approval.APPROVE, approval.RELEASE} == AGENT_MUST_NOT
    assert not imported_modules(SRC / "quote_approval.py") & AGENT_MODULES
    importers = [
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
        if "rfq_copilot.quote_approval" in imported_modules(p)
    ]  # fmt: skip
    assert importers == ["api/decision_app.py"]  # K5: only the human decision process
    callers = [
        p.relative_to(SRC) for p in SRC.rglob("*.py")
        if p.name != "quote_approval.py" and any(f"{n}(" in p.read_text() for n in ("release_quote", "assess_release"))
    ]  # fmt: skip
    assert callers == []


def test_release_checks_the_runtime_role_and_rereads_grants_first():
    source = inspect.getsource(release_quote)
    order = [source.index(s) for s in ("require_decision_runtime(session)", "drafts._authorize(", "_release_checked(", "separation_of_duties", "begin_nested()")]  # fmt: skip
    assert order == sorted(order)
    assert "principal.permissions" not in inspect.getsource(approval)
    assert "commit()" not in inspect.getsource(approval)
