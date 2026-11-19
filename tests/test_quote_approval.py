"""integrity offline: request validation, idempotency digests, result types and isolation of
``rfq_copilot.quote_approval``. No database: every refusal here happens before any read."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_quote_drafts import AGENT_MODULES, PRINCIPAL, SRC, imported_modules

from rfq_copilot import quote_approval as approval
from rfq_copilot.api.auth import Principal
from rfq_copilot.quote_approval import (
    ContextAttestation,
    QuoteDecision,
    approve_quote,
    decision_digest,
    reject_quote,
    submit_for_review,
)
from rfq_copilot.quote_drafts import DraftRefused
from rfq_copilot.services.evaluation import AGENT_MUST_NOT

SHA = "a" * 64
ATTEST = ContextAttestation(SHA, date(2027, 1, 21), True)
GOOD = dict(expected_version=1, idempotency_key="k", correlation_id="c")


class NoDatabase:
    """A session that fails the test if anything reaches it."""

    def __getattr__(self, name):
        raise AssertionError(f"database access ({name}) before validation")


def refused(reason, coroutine):
    with pytest.raises(DraftRefused) as caught:
        asyncio.run(coroutine)
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    "principal,quote_id,changes",
    [(Principal("CUST-001", frozenset()), "QUOTE-1", {}),  # no verified user
     (PRINCIPAL, "", {}),
     (PRINCIPAL, "Q" * 65, {}),
     (PRINCIPAL, "QUOTE-1", {"expected_version": 0}),
     (PRINCIPAL, "QUOTE-1", {"expected_version": "1"}),
     (PRINCIPAL, "QUOTE-1", {"expected_version": True}),
     (PRINCIPAL, "QUOTE-1", {"idempotency_key": ""}),
     (PRINCIPAL, "QUOTE-1", {"idempotency_key": " k"}),
     (PRINCIPAL, "QUOTE-1", {"idempotency_key": "k" * 257}),
     (PRINCIPAL, "QUOTE-1", {"idempotency_key": "kéy"}),
     (PRINCIPAL, "QUOTE-1", {"correlation_id": " "}),
     ("not a principal", "QUOTE-1", {})],
)  # fmt: skip
def test_invalid_requests_are_refused_before_any_database_access(principal, quote_id, changes):
    kwargs = {**GOOD, **changes}
    refused("invalid_request", submit_for_review(NoDatabase(), principal, quote_id, **kwargs))
    refused("invalid_request", approve_quote(NoDatabase(), principal, quote_id, attestation=ATTEST, **kwargs))  # fmt: skip
    refused("invalid_request", reject_quote(NoDatabase(), principal, quote_id, reason="no", **kwargs))  # fmt: skip


@pytest.mark.parametrize(
    "attestation",
    [None, {"rfq_source_sha256": SHA}, ContextAttestation("A" * 64, None, True),
     ContextAttestation(SHA[:63], None, True), ContextAttestation(SHA, None, False),
     ContextAttestation(SHA, None, 1), ContextAttestation(SHA, "2027-01-21", True),
     SimpleNamespace(rfq_source_sha256=SHA, requested_delivery_date=None, no_contradictions=True)],
)  # fmt: skip
def test_an_attestation_is_explicit_typed_and_bound_to_a_source_digest(attestation):
    refused("invalid_request", approve_quote(NoDatabase(), PRINCIPAL, "QUOTE-1", attestation=attestation, **GOOD))  # fmt: skip


@pytest.mark.parametrize("reason", ["", "   ", "x" * 1001, "line\nbreak", 42])
def test_a_rejection_needs_a_printable_reason(reason):
    refused("invalid_request", reject_quote(NoDatabase(), PRINCIPAL, "QUOTE-1", reason=reason, **GOOD))  # fmt: skip


def test_digests_separate_operations_actors_versions_and_decision_inputs():
    base = decision_digest(PRINCIPAL, "QUOTE-1", 1, approval.APPROVE, {"attestation": ATTEST.to_json()})  # fmt: skip
    assert base == decision_digest(PRINCIPAL, "QUOTE-1", 1, approval.APPROVE, {"attestation": ATTEST.to_json()})  # fmt: skip
    others = [
        decision_digest(PRINCIPAL, "QUOTE-1", 1, approval.REJECT, {"attestation": ATTEST.to_json()}),
        decision_digest(PRINCIPAL, "QUOTE-1", 2, approval.APPROVE, {"attestation": ATTEST.to_json()}),
        decision_digest(PRINCIPAL, "QUOTE-2", 1, approval.APPROVE, {"attestation": ATTEST.to_json()}),
        decision_digest(Principal("CUST-002", frozenset(), user_id="USER-001"), "QUOTE-1", 1, approval.APPROVE, {"attestation": ATTEST.to_json()}),  # fmt: skip
        decision_digest(Principal("CUST-001", frozenset(), user_id="USER-002"), "QUOTE-1", 1, approval.APPROVE, {"attestation": ATTEST.to_json()}),  # fmt: skip
        decision_digest(PRINCIPAL, "QUOTE-1", 1, approval.APPROVE, {"attestation": ContextAttestation(SHA, None, True).to_json()}),  # fmt: skip
        decision_digest(PRINCIPAL, "QUOTE-1", 1, approval.SUBMIT, {}),
    ]  # fmt: skip
    assert len({base, *others}) == len(others) + 1


def test_a_decision_round_trips_through_its_receipt_and_is_customer_safe():
    decision = QuoteDecision(
        quote_id="QUOTE-1", rfq_id="RFQ-1", customer_id="CUST-001", version=1,
        action="approved", status="approved", approval_request_id="APR-1",
        quote_row_version=3, actor_id="USER-009", assessment_fingerprint=SHA,
    )  # fmt: skip
    stored = decision.to_json()
    assert "replayed" not in stored
    again = QuoteDecision.from_json(stored)
    assert again.replayed is True and again.to_json() == stored
    text = repr(decision) + repr(stored)
    assert "cost" not in text and "margin" not in text and "labor" not in text


def test_separation_of_duties_covers_creator_version_creator_and_requester():
    me = Principal("CUST-001", frozenset(), user_id="USER-009")
    quote = SimpleNamespace(created_by="USER-001")
    version = SimpleNamespace(created_by="USER-002")
    request = SimpleNamespace(requested_by="USER-003")
    approval._separated(me, quote, version, request)  # distinct from all three
    for who in ("USER-001", "USER-002", "USER-003"):
        with pytest.raises(DraftRefused, match="separation_of_duties"):
            approval._separated(Principal("CUST-001", frozenset(), user_id=who), quote, version, request)  # fmt: skip


def test_decisions_are_human_only_and_isolated_from_agents():
    path = SRC / "quote_approval.py"
    imported = imported_modules(path)
    assert not imported & AGENT_MODULES
    assert not {m for m in imported if m.startswith(("rfq_copilot.mcp", "rfq_copilot.agents"))}
    assert {approval.APPROVE, "release_quote"} == AGENT_MUST_NOT
    # The ordinary staff router may submit only. Binding decisions remain indirect
    # calls in the separate decision application; agents and MCP cannot invoke them.
    callers = [
        p.relative_to(SRC) for p in SRC.rglob("*.py")
        if p.name != "quote_approval.py"
        and any(f"{name}(" in p.read_text() for name in ("approve_quote", "reject_quote", "submit_for_review"))
    ]  # fmt: skip
    assert [str(p) for p in callers] == ["api/routers/staff_quotes.py"]
    router = (SRC / "api/routers/staff_quotes.py").read_text()
    assert "approve_quote(" not in router and "reject_quote(" not in router
    source = path.read_text()
    assert "require_decision_runtime(session)" in source  # K5: before any read, for decisions
    assert "principal.permissions" not in source  # grants are re-read, never trusted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "permission_denied",
        "not_found",
        "separation_of_duties",
        "attestation_mismatch",
        "not_approvable",
        "rule_version_changed",
        "provenance_missing",
        "quote_expired",
    ],
)
async def test_receipt_recheck_never_replaces_security_or_evidence_refusals(monkeypatch, reason):
    replay = AsyncMock(side_effect=AssertionError("A legitimate refusal reached receipt replay"))
    monkeypatch.setattr(approval, "_replay", replay)
    result = await approval._replay_after_serialization(
        NoDatabase(), PRINCIPAL, "k", SHA, approval.APPROVE, DraftRefused(reason)
    )
    assert result is None
    replay.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [approval.SUBMIT, approval.APPROVE, approval.REJECT])
@pytest.mark.parametrize("reason", ["stale_version", "not_assessable", "not_pending"])
async def test_serialized_receipt_recheck_requires_current_authority(
    monkeypatch, operation, reason
):
    authorize = AsyncMock(side_effect=DraftRefused("permission_denied"))
    replay = AsyncMock(side_effect=AssertionError("Revoked authority reached receipt replay"))
    monkeypatch.setattr(approval.drafts, "_authorize", authorize)
    monkeypatch.setattr(approval, "_replay", replay)
    with pytest.raises(DraftRefused, match="permission_denied"):
        await approval._replay_after_serialization(
            NoDatabase(), PRINCIPAL, "k", SHA, operation, DraftRefused(reason)
        )
    authorize.assert_awaited_once()
    replay.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [approval.APPROVE, approval.REJECT])
async def test_serialized_decision_replay_still_requires_decision_role_and_approver(
    monkeypatch, operation
):
    from rfq_copilot.database import PrivilegedRuntimeRole

    monkeypatch.setattr(approval.drafts, "_authorize", AsyncMock())
    guard = AsyncMock(side_effect=PrivilegedRuntimeRole("unsafe_role"))
    approver = AsyncMock(side_effect=DraftRefused("permission_denied"))
    replay = AsyncMock(side_effect=AssertionError("Invalid approver reached receipt replay"))
    monkeypatch.setattr(approval, "require_decision_runtime", guard)
    monkeypatch.setattr(approval, "_approver", approver)
    monkeypatch.setattr(approval, "_replay", replay)
    with pytest.raises(PrivilegedRuntimeRole):
        await approval._replay_after_serialization(
            NoDatabase(), PRINCIPAL, "k", SHA, operation, DraftRefused("not_assessable")
        )
    approver.assert_not_awaited()
    guard.side_effect = None
    with pytest.raises(DraftRefused, match="permission_denied"):
        await approval._replay_after_serialization(
            NoDatabase(), PRINCIPAL, "k", SHA, operation, DraftRefused("not_assessable")
        )
    replay.assert_not_awaited()
