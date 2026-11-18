"""K5 (Day 55) offline: the decision-authority revision, its contract pins, the decision
allowlists and provisioning artifact, the decision credential and the separate human
decision application. No database."""

import hashlib
import importlib.util
import inspect
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_auth import trust as trust
from test_history_immutability import URL, environment
from test_quote_drafts import AGENT_MODULES, SRC, imported_modules

from rfq_copilot import schema_contract
from rfq_copilot.api import decision_app
from rfq_copilot.api.app import create_app
from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
from rfq_copilot.database import (
    DECISION_GROUP,
    DECISION_INSERTABLE,
    DECISION_UPDATABLE,
    RUNTIME_UPDATABLE,
)

ROOT = Path(__file__).resolve().parents[1]
K5_FILE = ROOT / "alembic" / "versions" / "20261116_04_k5_decision_authority.py"
K5_SQL = ROOT / "docs" / "database" / "k5_decision_roles.sql"
K5_GUARDS = {"k5_quotes_guard", "k5_quote_versions_guard", "k5_approval_requests_guard", "k5_approval_history_guard"}  # fmt: skip
K5_BINDINGS = {"k5_approval_history_bound", "k5_approval_request_bound"}  # deferred, at COMMIT
K5_TRIGGERS = K5_GUARDS | K5_BINDINGS
REPLACED = {"k4_quotes_guard", "k4_quote_versions_guard", "k4_approval_requests_guard"}


def k5():
    spec = importlib.util.spec_from_file_location("k5_under_test", K5_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_is_the_head_on_top_of_k4_and_pinned_independently():
    module = k5()
    assert (module.revision, module.down_revision) == ("20261116_04", "20261116_03")
    measured = hashlib.sha256(K5_FILE.read_bytes()).hexdigest()
    assert schema_contract.GUARD_FILE_SHA256["20261116_04"] == measured, f"pin {measured}"
    current, k4 = schema_contract.K5, schema_contract.K4
    assert current.revision == "20261116_04"
    # The customer role (20261117_01) on top of K5 adds no guard: K5's allowlist stays active.
    assert schema_contract.active().guards_revisions == current.guards_revisions
    assert current.guards_revisions == (*k4.guards_revisions, "20261116_04")
    # Keys, CHECKs and indexes unchanged; the constraint triggers change the measured shape.
    assert current.catalog_counts == k4.catalog_counts
    assert current.shape_digest != k4.shape_digest
    source = K5_FILE.read_text()
    assert not any(s in source for s in ("create_table", "ADD COLUMN", "ADD CONSTRAINT", "CREATE INDEX", "DROP COLUMN", "GRANT ", "CREATE ROLE"))  # fmt: skip


def test_the_allowlist_replaces_the_k4_triggers_and_guards_approval_history():
    module = k5()
    assert {t[0] for t in module.DROPPED_TRIGGERS} == REPLACED
    assert {t[0] for t in module.TRIGGERS} == set(module.FUNCTIONS) == K5_TRIGGERS
    functions, triggers = schema_contract.contract_guards(schema_contract.CURRENT)
    names = {t[0] for t in triggers}
    assert len(triggers) == 18 and K5_TRIGGERS <= names and not REPLACED & names
    assert set(functions) >= K5_TRIGGERS | REPLACED  # K4 functions kept for the downgrade
    by_table = {(t[1], t[0], t[3], t[4]) for t in triggers}
    assert {("approval_history", "k5_approval_history_guard", ("INSERT",), "before"),
            ("approval_history", "k5_approval_history_bound", ("INSERT",), "deferred"),
            ("approval_history", "k2_approval_history_immutable", ("UPDATE", "DELETE"), "before"),
            ("approval_requests", "k5_approval_requests_guard", ("INSERT", "UPDATE", "DELETE"), "before"),
            ("approval_requests", "k5_approval_request_bound", ("UPDATE",), "deferred")} <= by_table  # fmt: skip
    assert sum(t[4] == "deferred" for t in triggers) == 4  # K3's two + K5's two
    assert set(schema_contract.dropped_triggers("20261116_04")) == REPLACED


def test_every_decision_needs_the_decision_group_and_is_bound_to_its_rows():
    functions = k5().FUNCTIONS
    assert DECISION_GROUP == "rfq_quote_decision"
    for name, body in functions.items():
        if name in K5_GUARDS:
            assert "to_regrole('rfq_quote_decision')" in body  # a missing group: refused
        assert "CONSTRAINT = TG_NAME" in body and "integrity_constraint_violation" in body
        assert "EXECUTE " not in body and "current_setting" not in body
    quotes = functions["k5_quotes_guard"]
    assert quotes.count("IF decision AND") == 3  # approve, reject, release; never submit
    for grant in ("'approve_quote'", "'reject_quote'", "'release_quote'"):
        assert grant in quotes
    assert "NEW.released_by <> OLD.approver_id" in quotes  # Day 55 policy
    assert quotes.count("v.expiry_date >= today") == 2  # approve and release, database time
    assert "r.requested_by <> NEW.approver_id AND v.created_by <> NEW.approver_id" in quotes
    assert "OLD.status = 'rejected' AND NEW.status = 'revised_draft'" in quotes
    assert "AND r.status = 'rejected') THEN" in quotes  # only a genuinely rejected quotation
    assert "IF decision AND OLD.is_current" in functions["k5_quote_versions_guard"]
    requests = functions["k5_approval_requests_guard"]
    assert "'request_approval'" in requests and "decision AND OLD.status = 'pending'" in requests
    assert "'superseded'" not in requests  # supersession stays blocked (no approved writer)
    history = functions["k5_approval_history_guard"]
    assert "q.approver_id <> NEW.actor_id" in history and "NEW.action = 'submitted'" in history
    source = K5_FILE.read_text()
    assert "SECURITY INVOKER" in source and "SECURITY DEFINER" not in source
    assert "ENABLE ALWAYS TRIGGER" in source and "allow_test_downgrade" in source


def test_decision_rows_are_bound_to_the_finalized_quotation_at_commit():
    module = k5()
    kinds = {t[0]: t[4] for t in module.TRIGGERS}
    assert {name for name, kind in kinds.items() if kind == "deferred"} == K5_BINDINGS
    assert "DEFERRABLE INITIALLY DEFERRED" in K5_FILE.read_text()
    history = module.FUNCTIONS["k5_approval_history_bound"]
    for action, state in (("'submitted'", "'pending_approval'"), ("'approved'", "'approved'"),
                          ("'rejected'", "'rejected'"), ("'released'", "'released'")):  # fmt: skip
        assert f"WHEN {action} THEN" in history and f"q.status = {state}" in history
    assert "q.released_by = NEW.actor_id" in history and "q.released_at = NEW.action_at" in history  # fmt: skip
    assert "q.approver_id = NEW.actor_id" in history and "ELSE false" in history
    request = module.FUNCTIONS["k5_approval_request_bound"]
    assert "h.actor_id = NEW.resolved_by" in request and "h.action_at = NEW.resolved_at" in request  # fmt: skip
    assert "ELSE false" in request
    # The services check the bindings inside their own savepoint, before they finish.
    from rfq_copilot import quote_approval

    source = inspect.getsource(quote_approval)
    assert source.count("await session.execute(DEFER_BINDINGS)") == 4  # each decision step
    assert "await session.execute(CHECK_BINDINGS)" in inspect.getsource(quote_approval._finish)
    assert set(quote_approval.DECISION_CONSTRAINTS.replace("business.", "").split(", ")) == K5_BINDINGS  # fmt: skip


def test_the_decision_allowlists_are_the_provisioning_artifact():
    sql = K5_SQL.read_text()
    block = sql.split("-- BEGIN K5 DECISION GRANTS\n")[1].split("-- END K5 DECISION GRANTS")[0]
    updates = re.findall(r"^GRANT UPDATE \(([^)]+)\) ON business\.(\w+) TO rfq_quote_decision", block, re.M)  # fmt: skip
    assert {
        f"{t}.{c.strip()}" for cols, t in updates for c in cols.split(",")
    } == DECISION_UPDATABLE
    inserts = re.findall(r"^GRANT INSERT ON (.+?) TO rfq_quote_decision", block, re.M)
    assert {t.strip().removeprefix("business.") for line in inserts for t in line.split(",")} == DECISION_INSERTABLE  # fmt: skip
    assert not re.search(r"^GRANT (DELETE|TRUNCATE|TRIGGER|REFERENCES|ALL)", block, re.M)
    assert not re.search(r"PASSWORD\s+'", sql, re.I)
    assert not re.search(r"GRANT rfq_quote_decision TO rfq_copilot_app", sql)
    # The app role keeps no decision power; the decision role creates no quotation content.
    assert not (DECISION_UPDATABLE - {"quotes.status", "quotes.row_version", "quotes.rejection_reason"}) & RUNTIME_UPDATABLE  # fmt: skip
    assert not {"quotes", "quote_versions", "quote_line_items", "approval_requests", "pricing_calculations"} & DECISION_INSERTABLE  # fmt: skip


def decision_environment(**changes):
    defaults = {
        "DECISION_DATABASE_URL": URL.format(role="decision_role", database="example_dev"),
        "TEST_DECISION_DATABASE_URL": URL.format(
            role="decision_test_role", database="example_test"
        ),
    }
    return environment(**{**defaults, **changes})


def test_the_decision_configuration_is_a_third_role_on_the_same_database():
    dev = DatabaseConfig.from_env("development", environ=decision_environment(), decision=True)
    assert (dev.url.username, dev.decision, dev.runtime, dev.url.database) == ("decision_role", True, False, "example_dev")  # fmt: skip
    testing = DatabaseConfig.from_env("test", environ=decision_environment(), decision=True)
    assert (testing.url.username, testing.url.database) == ("decision_test_role", "example_test")
    assert "k2-secret" not in repr(dev) and "k2-secret" not in str(dev)


@pytest.mark.parametrize(
    "target,changes,flags",
    [("development", {"DECISION_DATABASE_URL": None}, {}),
     ("test", {"TEST_DECISION_DATABASE_URL": None}, {}),
     ("development", {"DECISION_DATABASE_URL": URL.format(role="owner_role", database="example_dev")}, {}),
     ("development", {"DECISION_DATABASE_URL": URL.format(role="app_role", database="example_dev")}, {}),
     ("test", {"TEST_DECISION_DATABASE_URL": URL.format(role="app_test_role", database="example_test")}, {}),
     ("development", {"DECISION_DATABASE_URL": URL.format(role="decision_role", database="example_test")}, {}),
     ("development", {}, {"runtime": True})],
)  # fmt: skip
def test_the_decision_configuration_fails_closed(target, changes, flags):
    with pytest.raises(DatabaseConfigurationError) as caught:
        DatabaseConfig.from_env(target, environ=decision_environment(**changes), decision=True, **flags)  # fmt: skip
    assert "k2-secret" not in str(caught.value) and "postgresql" not in str(caught.value)


def test_the_main_api_never_mounts_decisions(trust):
    paths = create_app(trust.settings()).openapi()["paths"]
    assert not any("decision" in path for path in paths)


def test_the_decision_app_has_only_permissioned_decision_routes():
    required = {
        route.path: {
            getattr(d.call, "required_permission", None) for d in route.dependant.dependencies
        } - {None}
        for route in decision_app.router.routes
    }  # fmt: skip
    assert required == {
        "/quote-decisions/{quote_id}/approval": {"approve_quote"},
        "/quote-decisions/{quote_id}/rejection": {"reject_quote"},
        "/quote-decisions/{quote_id}/release": {"release_quote"},
    }


def test_the_decision_app_refuses_unauthenticated_requests_before_any_database_use(trust):
    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError(f"database access ({name}) before authentication")

    app = decision_app.create_decision_app(trust.settings(), database=NoDatabase())
    body = {"expected_version": 1, "idempotency_key": "k"}
    with TestClient(app) as client:
        for path in ("release", "rejection", "approval"):
            response = client.post(f"/api/v1/quote-decisions/Q-1/{path}", json=body)
            assert response.status_code == 401, path
        forged = client.post(
            "/api/v1/quote-decisions/Q-1/release",
            json=body, headers={"X-User-Id": "U-1", "X-Role": "sales_manager"},
        )  # fmt: skip
        assert forged.status_code == 401


@pytest.mark.parametrize("field", ["approver_id", "actor_id", "user_id", "customer_id", "role"])
def test_decision_bodies_never_carry_an_identity(field):
    good = {"expected_version": 1, "idempotency_key": "k", "rfq_source_sha256": "a" * 64, "no_contradictions": True}  # fmt: skip
    decision_app.ApprovalRequestBody(**good)
    with pytest.raises(ValidationError):
        decision_app.ApprovalRequestBody(**good, **{field: "U-1"})
    with pytest.raises(ValidationError):
        decision_app.ReleaseRequestBody(expected_version=1, idempotency_key="k", **{field: "U-1"})
    with pytest.raises(ValidationError):
        decision_app.ApprovalRequestBody(**{**good, "no_contradictions": False})


def test_the_decision_credential_is_read_only_by_the_decision_process():
    readers = sorted(
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "DECISION_DATABASE_URL" in p.read_text()
    )  # fmt: skip
    assert readers == ["api/decision_app.py", "config.py"]
    openers = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if 'from_serving_env("development", decision=True)' in p.read_text()]  # fmt: skip
    assert openers == ["api/decision_app.py"]
    importers = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "rfq_copilot.api.decision_app" in imported_modules(p)]  # fmt: skip
    assert importers == []  # served on its own (uvicorn --factory), mounted by nothing else
    assert not imported_modules(SRC / "api" / "decision_app.py") & AGENT_MODULES
    assert not {m for m in imported_modules(SRC / "api" / "decision_app.py") if m.startswith(("rfq_copilot.mcp", "rfq_copilot.agents"))}  # fmt: skip
