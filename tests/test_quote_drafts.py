"""Day 51 quote-draft persistence: offline checks (no database, network or model)."""

import ast
import inspect
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.naming import conv

import rfq_copilot.quote_drafts as quote_drafts
from rfq_copilot.api.auth import Principal
from rfq_copilot.graph import READ_ONLY_TOOLS
from rfq_copilot.mcp.contracts import TOOLS
from rfq_copilot.quote_drafts import (
    DraftRefused,
    QuoteDraft,
    _checked,
    _valid_request,
    _workflow_allows_drafts,
    create_quote_draft,
    request_digest,
    violated_unique,
)
from rfq_copilot.repositories.idempotency import UPLOAD_OPERATION, add_receipt, find_receipt

SRC = Path(quote_drafts.__file__).resolve().parent
INITIAL = SRC.parents[1] / "alembic/versions/20261108_01_initial_rfq_database_schema.py"
MONDAY = date(2026, 11, 9)
PRINCIPAL = Principal("CUST-001", frozenset(), user_id="USER-001")


def inputs(**changes):
    return {
        "horizon_start": MONDAY,
        "requested_delivery_date": None,
        "contradictions": (),
        "inventory_snapshot": None,
        "capacity_snapshot": None,
        "expected_evaluation_fingerprint": None,
        **changes,
    }


def draft(**changes):
    return replace(
        QuoteDraft(
            quote_id="QUOTE-1",
            rfq_id="RFQ-1",
            customer_id="CUST-001",
            version=1,
            status="draft",
            currency="USD",
            total_usd=Decimal("998.98"),
            calculation_id="CALC-1",
            evaluation_fingerprint="a" * 64,
            outcome="quote_draft_ready_for_approval",
            manager_review_required=False,
            manager_review_reasons=(),
            rule_versions=("mfg-v1.0", "price-v1.0", "approval-v1.2", "workflow-v1.0"),
        ),
        **changes,
    )


def evaluation(**changes):
    """A duck-typed draftable evaluation: two lines of 400.00 and 598.98."""
    line = lambda pid, total: SimpleNamespace(  # noqa: E731
        line_total_usd=Decimal(total), cost=SimpleNamespace(part_id=pid, quantity=5)
    )
    pricing = SimpleNamespace(
        result="priced", currency="USD", lines=(line("P1", "400.00"), line("P2", "598.98"))
    )
    quotation = SimpleNamespace(
        result="quoted",
        pricing=pricing,
        quote_total_usd=Decimal("998.98"),
        parts=(SimpleNamespace(part_id="P1"), SimpleNamespace(part_id="P2")),
        lead_time=SimpleNamespace(snapshots=object()),
    )
    base = dict(
        draft_creation_allowed=True,
        evaluation_fingerprint="b" * 64,
        quotation=quotation,
        human_release_approval_required=True,
        approval_must_be_human=True,
        quote_release_authorized_for_agent=False,
        customer=SimpleNamespace(customer_id="CUST-001"),
    )
    return SimpleNamespace(**{**base, **changes})


# --- Request validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "principal, rfq_id, key, correlation, changes",
    [
        (Principal("CUST-001"), "RFQ-1", "k", "c", {}),  # no verified actor
        ("CUST-001", "RFQ-1", "k", "c", {}),  # not a Principal at all
        (PRINCIPAL, "", "k", "c", {}),
        (PRINCIPAL, "R" * 65, "k", "c", {}),
        (PRINCIPAL, "RFQ-1", "", "c", {}),
        (PRINCIPAL, "RFQ-1", "k" * 257, "c", {}),
        (PRINCIPAL, "RFQ-1", " k", "c", {}),
        (PRINCIPAL, "RFQ-1", "kéy", "c", {}),
        (PRINCIPAL, "RFQ-1", "k\n", "c", {}),
        (PRINCIPAL, "RFQ-1", "k", " ", {}),
        (PRINCIPAL, "RFQ-1", "k", "c", {"horizon_start": "2026-11-09"}),
        (PRINCIPAL, "RFQ-1", "k", "c", {"requested_delivery_date": "2027-01-21"}),
        (PRINCIPAL, "RFQ-1", "k", "c", {"contradictions": ("",)}),
        (PRINCIPAL, "RFQ-1", "k", "c", {"inventory_snapshot": ""}),
        (PRINCIPAL, "RFQ-1", "k", "c", {"expected_evaluation_fingerprint": "A" * 64}),
    ],
)
def test_invalid_requests_are_refused_before_any_database_access(
    principal, rfq_id, key, correlation, changes
):
    with pytest.raises(DraftRefused) as refused:
        _valid_request(principal, rfq_id, key, correlation, inputs(**changes))
    assert refused.value.reason == "invalid_request"


def test_a_valid_request_passes():
    _valid_request(PRINCIPAL, "RFQ-1", "retry-1", "corr-1", inputs(contradictions=("x",)))


@pytest.mark.asyncio
async def test_a_single_string_of_contradictions_is_refused_without_a_session():
    with pytest.raises(DraftRefused) as refused:
        await create_quote_draft(
            None, PRINCIPAL, "RFQ-1", idempotency_key="k", correlation_id="c",
            horizon_start=MONDAY, contradictions="conflict",
        )  # fmt: skip
    assert refused.value.reason == "invalid_request"


# --- Idempotency identity -------------------------------------------------------------------


def test_request_digest_binds_tenant_rfq_and_every_evaluation_input():
    base = request_digest(PRINCIPAL, "RFQ-1", inputs())
    assert base == request_digest(PRINCIPAL, "RFQ-1", inputs())
    other_tenant = Principal("CUST-002", frozenset(), user_id="USER-001")
    variants = [
        request_digest(other_tenant, "RFQ-1", inputs()),
        request_digest(PRINCIPAL, "RFQ-2", inputs()),
        request_digest(PRINCIPAL, "RFQ-1", inputs(horizon_start=date(2026, 11, 16))),
        request_digest(PRINCIPAL, "RFQ-1", inputs(requested_delivery_date=date(2027, 1, 21))),
        request_digest(PRINCIPAL, "RFQ-1", inputs(contradictions=("x",))),
        request_digest(PRINCIPAL, "RFQ-1", inputs(capacity_snapshot="SNAP")),
        request_digest(PRINCIPAL, "RFQ-1", inputs(expected_evaluation_fingerprint="c" * 64)),
    ]
    assert base not in variants and len(set(variants)) == len(variants)


def test_receipt_round_trips_as_a_replay_and_stays_non_binding():
    original = draft(manager_review_reasons=("QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD",))
    stored = original.to_json()
    assert "replayed" not in stored and stored["total_usd"] == "998.98"
    again = QuoteDraft.from_json(stored)
    assert again == replace(original, replayed=True)
    assert (again.human_approval_required, again.approved, again.released) == (True, False, False)
    with pytest.raises(FrozenInstanceError):
        again.approved = True  # type: ignore[misc]


def test_receipt_carries_no_internal_cost_or_margin():
    keys = set(draft().to_json())
    assert not {k for k in keys if "cost" in k or "margin" in k or "breakdown" in k}


def test_idempotency_repository_defaults_stay_the_upload_operation():
    # Day 51 added an operation parameter; existing upload callers are unchanged.
    for function in (find_receipt, add_receipt):
        assert inspect.signature(function).parameters["operation"].default == UPLOAD_OPERATION
    assert quote_drafts.OPERATION == "create_quote_draft"


# --- Trusted evaluation gate ----------------------------------------------------------------


def test_a_draftable_evaluation_is_accepted():
    quotation, pricing = _checked(evaluation(), PRINCIPAL, None)
    assert quotation.quote_total_usd == Decimal("998.98") and pricing.result == "priced"
    _checked(evaluation(), PRINCIPAL, "b" * 64)  # the reviewed fingerprint matches


def test_not_draftable_and_changed_evaluations_are_refused():
    with pytest.raises(DraftRefused) as refused:
        _checked(evaluation(draft_creation_allowed=False), PRINCIPAL, None)
    assert refused.value.reason == "not_draftable"
    with pytest.raises(DraftRefused) as refused:
        _checked(evaluation(), PRINCIPAL, "c" * 64)
    assert refused.value.reason == "evaluation_changed"


def _tampered():
    def with_quotation(**q):
        e = evaluation()
        return evaluation(quotation=SimpleNamespace(**{**vars(e.quotation), **q}))

    def with_pricing(**p):
        e = evaluation()
        return with_quotation(pricing=SimpleNamespace(**{**vars(e.quotation.pricing), **p}))

    line = lambda total, qty=5, pid="P1": SimpleNamespace(  # noqa: E731
        line_total_usd=Decimal(total), cost=SimpleNamespace(part_id=pid, quantity=qty)
    )
    ok2 = line("598.98", pid="P2")
    return {
        "agent_may_release": evaluation(quote_release_authorized_for_agent=True),
        "approval_not_human": evaluation(approval_must_be_human=False),
        "no_human_release": evaluation(human_release_approval_required=False),
        "other_customer": evaluation(customer=SimpleNamespace(customer_id="CUST-002")),
        "no_quotation": evaluation(quotation=None),
        "not_quoted": with_quotation(result="needs_clarification"),
        "no_pricing": with_quotation(pricing=None),
        "not_priced": with_pricing(result="needs_clarification"),
        "currency": with_pricing(currency="EUR"),
        "no_snapshots": with_quotation(lead_time=SimpleNamespace(snapshots=None)),
        "float_total": with_quotation(quote_total_usd=998.98),
        "nan_total": with_quotation(quote_total_usd=Decimal("NaN")),
        "negative_total": with_quotation(quote_total_usd=Decimal("-1")),
        "total_mismatch": with_quotation(quote_total_usd=Decimal("999.00")),
        "negative_line": with_pricing(lines=(line("-1.00"), line("999.98", pid="P2"))),
        "zero_quantity": with_pricing(lines=(line("400.00", qty=0), ok2)),
        "missing_line": with_pricing(lines=(line("998.98"),)),
        "swapped_parts": with_pricing(lines=(line("598.98", pid="P2"), line("400.00"))),
    }


@pytest.mark.parametrize("case", sorted(_tampered()))
def test_tampered_or_inconsistent_evaluations_fail_closed(case):
    with pytest.raises(DraftRefused) as refused:
        _checked(_tampered()[case], PRINCIPAL, None)
    assert refused.value.reason == "invalid_evaluation"


def test_the_pinned_workflow_must_allow_drafts_as_idempotent_writes():
    good = SimpleNamespace(
        document={"idempotent_writes": ["create_quote_draft"], "transitions": {"draft": []}}
    )
    _workflow_allows_drafts(good)
    bad = [
        {"idempotent_writes": [], "transitions": {"draft": []}},
        {"idempotent_writes": ["create_quote_draft"], "transitions": {}},
    ]
    for document in bad:
        with pytest.raises(DraftRefused):
            _workflow_allows_drafts(SimpleNamespace(document=document))


# --- Boundaries -----------------------------------------------------------------------------


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def called_names(path: Path) -> set[str]:
    """Names of everything called: ``f(...)`` -> f, ``x.y.f(...)`` -> f."""
    names = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return names


def imported_modules(path: Path) -> set[str]:
    """Every module imported anywhere (lazy imports included), with ``from a import b``
    recorded as both ``a`` and ``a.b`` so a submodule import is never missed."""
    modules = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative: resolve against the package
                base = ".".join(["rfq_copilot", *filter(None, [base])])
            modules |= {base, *(f"{base}.{alias.name}" for alias in node.names)}
    return modules


AGENT_MODULES = frozenset(
    f"rfq_copilot.{name}"
    for name in ("supervisor", "graph", "agent_integration", "recovery", "intake",
                 "manufacturing_agent", "supply_agent", "pricing_agent", "quotation_agent")
)  # fmt: skip


def test_service_never_commits_prints_logs_or_imports_agents():
    service = Path(quote_drafts.__file__)
    assert not called_names(service) & {"commit", "print", "getLogger"}
    modules = imported_modules(service)
    assert not {m for m in modules if m == "logging" or m.startswith("logging.")}
    assert not modules & AGENT_MODULES  # evaluation is recomputed, never read from agents


def test_the_ast_checks_detect_what_they_guard(tmp_path):
    # fingerprint(...) is not print(...): the false positive of the substring check.
    probe = tmp_path / "probe.py"
    probe.write_text("fingerprint(x)\n")
    assert called_names(probe) == {"fingerprint"}
    probe.write_text("def f(s):\n    import logging\n    print(1)\n    s.commit()\n")
    assert {"print", "commit"} <= called_names(probe) and "logging" in imported_modules(probe)
    probe.write_text("from rfq_copilot import quote_drafts\n")
    assert "rfq_copilot.quote_drafts" in imported_modules(probe)


@pytest.mark.parametrize(
    "module",
    ["supervisor.py", "graph.py", "agent_integration.py", "recovery.py", "ownership.py",
     "observability.py", "escalation.py", "quotation_agent.py"],
)  # fmt: skip
def test_graph_evaluation_has_no_persistence_side_effect(module):
    # Supervisor and agents stay advisory: nothing in them can create a draft.
    assert "rfq_copilot.quote_drafts" not in imported_modules(SRC / module)


def test_no_mcp_tool_can_create_a_draft():
    names = {tool.name for tool in TOOLS} | set(READ_ONLY_TOOLS)
    assert not {n for n in names if "draft" in n or n.startswith(("create_", "approve", "release"))}


# --- Integrity-error classification ---------------------------------------------------------


def integrity(sqlstate, constraint):
    driver = SimpleNamespace(constraint_name=constraint)  # asyncpg's exception attribute
    adapted = SimpleNamespace(sqlstate=sqlstate, orig=driver)
    return IntegrityError("INSERT ...", {}, adapted)


def test_only_named_unique_violations_are_classified():
    assert violated_unique(integrity("23505", quote_drafts.QUOTE_PER_RFQ)) == "uq_quotes_rfq_id"
    assert violated_unique(integrity("23505", quote_drafts.RECEIPT_KEY)) == quote_drafts.RECEIPT_KEY
    # Foreign key, CHECK and NOT NULL violations are never read as a won race.
    for sqlstate in ("23503", "23514", "23502"):
        assert violated_unique(integrity(sqlstate, quote_drafts.QUOTE_PER_RFQ)) is None
    assert violated_unique(IntegrityError("INSERT ...", {}, Exception("no sqlstate"))) is None


def test_constraint_names_match_the_approved_migration():
    migration = INITIAL.read_text()
    assert 'name=op.f("uq_quotes_rfq_id")' in migration
    # The historical migration keeps its declared 69-character name; PostgreSQL stores the
    # name SQLAlchemy's identifier formatting derives from it (63-character limit).
    declared = "uq_idempotency_records_actor_id_customer_id_operation_idempotency_key"
    assert f'name=op.f("{declared}")' in migration
    preparer = postgresql.dialect().identifier_preparer
    stored = preparer.format_constraint(UniqueConstraint(name=conv(declared)), _alembic_quote=False)
    assert stored == quote_drafts.RECEIPT_KEY
    # The pointer the service defers is the one created DEFERRABLE INITIALLY DEFERRED.
    schema, name = quote_drafts.CURRENT_VERSION_FK.split(".")
    assert schema == "business" and name == "fk_quotes_quote_id_current_version_quote_versions"
    assert 'op.f(f"fk_quotes_quote_id_{column}_quote_versions")' in migration
    assert 'deferrable=True,\n            initially="DEFERRED"' in migration
