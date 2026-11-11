"""Phase 3.5 completion: public tests (no PostgreSQL, no dataset).

Policy versions, explicit customer context, token revocation wiring, upload idempotency
key handling, deep immutability (TD-026), missing-file listing and the operator CLI's
argument contract. The PostgreSQL side is test_phase35_completion_database.py.
"""

import json
import os

import pytest
from pydantic import ValidationError
from test_attachments import fakes as fakes
from test_attachments import make_app, pdf_file, stored, upload
from test_auth import PROTECTED, SUBJECT, FakeDatabase, api, bearer, call
from test_auth import identity as identity
from test_auth import trust as trust
from test_phase3_hardening import storage, stored_file
from test_rbac import MATRIX, UNRELATED, concrete

import rfq_copilot.repositories as repositories
from rfq_copilot import identity_admin
from rfq_copilot import reconcile_attachments as reconciler
from rfq_copilot.api.auth import (
    ATTACH_RFQ_DOCUMENT,
    CREATE_RFQ,
    ROUTE_PERMISSIONS,
    VIEW_ASSIGNED_RFQS,
    VIEW_CATALOG,
    VIEW_CUSTOMER,
    VIEW_QUOTE,
    Principal,
    require_route_permission,
)
from rfq_copilot.api.schemas.reads import MaterialPage, MaterialSummary
from rfq_copilot.api.schemas.rfq import RfqSubmissionRequest

# Route template -> operation, the single mapping both policy tables are keyed by.
OPERATION = {
    ("GET", "/api/v1/customers/me"): "customer_profile",
    ("GET", "/api/v1/rfqs"): "rfq_read",
    ("GET", "/api/v1/rfqs/{rfq_id}"): "rfq_read",
    ("GET", "/api/v1/materials"): "catalog_read",
    ("GET", "/api/v1/quotes/{quote_id}"): "quote_read",
    ("GET", "/api/v1/rfqs/{rfq_id}/attachments"): "attachment_list",
    ("POST", "/api/v1/rfqs/{rfq_id}/attachments"): "attachment_upload",
}
V2 = {
    ("GET", "/api/v1/customers/me"): VIEW_CUSTOMER,
    ("GET", "/api/v1/rfqs"): VIEW_ASSIGNED_RFQS,
    ("GET", "/api/v1/rfqs/{rfq_id}"): VIEW_ASSIGNED_RFQS,
    ("GET", "/api/v1/materials"): VIEW_CATALOG,
    ("GET", "/api/v1/quotes/{quote_id}"): VIEW_QUOTE,
    ("GET", "/api/v1/rfqs/{rfq_id}/attachments"): VIEW_ASSIGNED_RFQS,
    ("POST", "/api/v1/rfqs/{rfq_id}/attachments"): ATTACH_RFQ_DOCUMENT,
}


def granted(identity, *permissions, user="U-1", customer="CUST-A"):
    identity.bindings[user] = customer
    identity.grants[user] = frozenset(permissions)


# --- Decision A: policy versions -----------------------------------------------------------


def test_policy_tables_cover_every_operation_and_v1_is_the_day21_matrix():
    assert set(OPERATION) == set(MATRIX) == set(V2)
    assert set(ROUTE_PERMISSIONS) == {"v1", "v2"}
    for policy in ROUTE_PERMISSIONS.values():
        assert set(policy) == set(OPERATION.values())
    assert {key: ROUTE_PERMISSIONS["v1"][op] for key, op in OPERATION.items()} == MATRIX
    assert {key: ROUTE_PERMISSIONS["v2"][op] for key, op in OPERATION.items()} == V2
    approval = {"request_approval", "approve_quote", "reject_quote", "release_quote"}
    assert not approval & {p for table in ROUTE_PERMISSIONS.values() for p in table.values()}


@pytest.mark.parametrize(("method", "template"), list(V2))
def test_v2_routes_need_exactly_their_dedicated_permission(trust, identity, tmp_path, method, template):  # fmt: skip
    required = V2[(method, template)]
    everything_else = {*MATRIX.values(), *V2.values(), *UNRELATED} - {required}
    app = api(trust, attachment_dir=tmp_path / "store", permission_policy="v2")
    granted(identity, *everything_else)
    assert call(app, method, concrete(template), headers=bearer(trust.mint())).status_code == 403
    granted(identity, required)
    assert call(app, method, concrete(template), headers=bearer(trust.mint())).status_code != 403


def test_v1_permissions_no_longer_open_dedicated_routes_under_v2(trust, identity, tmp_path):
    # The escalation the audit worried about: view_assigned_rfqs and create_rfq must not
    # keep opening the catalog, quotes or uploads once v2 is active.
    granted(identity, VIEW_ASSIGNED_RFQS, CREATE_RFQ, VIEW_CUSTOMER)
    app = api(trust, attachment_dir=tmp_path / "store", permission_policy="v2")
    for key in [("GET", "/api/v1/materials"), ("GET", "/api/v1/quotes/{quote_id}"), ("POST", "/api/v1/rfqs/{rfq_id}/attachments")]:  # fmt: skip
        assert call(app, key[0], concrete(key[1]), headers=bearer(trust.mint())).status_code == 403


def test_upload_under_v2_without_permission_reads_nothing(trust, identity, tmp_path):
    granted(identity, CREATE_RFQ, VIEW_ASSIGNED_RFQS)
    app = api(trust, attachment_dir=tmp_path / "store", permission_policy="v2")
    response = call(app, "POST", "/api/v1/rfqs/RFQ-1/attachments", headers=bearer(trust.mint()),
                    files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})  # fmt: skip
    assert response.status_code == 403
    assert not (tmp_path / "store").exists() and app.state.write_database is None


def test_unknown_route_operation_fails_at_registration():
    with pytest.raises(KeyError):
        require_route_permission("catalog_write")


def test_policy_setting_is_validated():
    from rfq_copilot.api.config import ApiSettings

    for field, value in (("permission_policy", "v3"), ("rfq_assignments", "on"), ("token_revocation", "yes"),
                         ("customer_context", "any"), ("upload_idempotency", "true")):  # fmt: skip
        with pytest.raises(ValidationError):
            ApiSettings(**{field: value})
    defaults = ApiSettings()
    assert (defaults.permission_policy, defaults.rfq_assignments, defaults.token_revocation,
            defaults.customer_context, defaults.upload_idempotency) == ("v1", "off", "off", "single", "off")  # fmt: skip


# --- Decision B: explicit customer context and assignment scope -------------------------------


@pytest.fixture
def multi(identity, monkeypatch):
    """U-1 is bound to two eligible customers."""
    customers = {"U-1": ("CUST-A", "CUST-B")}

    async def eligible(session, user):
        return customers.get(user.user_id, ())

    monkeypatch.setattr(repositories, "eligible_customers", eligible)
    identity.grants["U-1"] = frozenset({VIEW_CUSTOMER})
    return customers


def probe_app(trust, **settings):
    from fastapi import Depends

    app = api(trust, **settings)
    seen = []

    @app.get("/probe")
    async def probe(principal: Principal = Depends(require_route_permission("customer_profile"))):  # noqa: B008
        seen.append(principal.customer_id)
        return {"customer": principal.customer_id}

    return app, seen


def test_selectable_context_requires_a_named_bound_customer(trust, multi):
    app, seen = probe_app(trust, customer_context="selectable")
    auth = bearer(trust.mint())
    assert call(app, "GET", "/probe", headers=auth).status_code == 403  # several: no default
    for customer in ("CUST-A", "CUST-B"):
        response = call(app, "GET", "/probe", headers={**auth, "X-RFQ-Customer-Context": customer})
        assert response.status_code == 200 and response.json() == {"customer": customer}
    for bad in ("CUST-C", "", " CUST-A", "CUST-A\t", "cust-a", "x" * 65):
        assert call(app, "GET", "/probe", headers={**auth, "X-RFQ-Customer-Context": bad}).status_code == 403  # fmt: skip
    duplicated = [("Authorization", auth["Authorization"]), ("X-RFQ-Customer-Context", "CUST-A"), ("X-RFQ-Customer-Context", "CUST-B")]  # fmt: skip
    assert call(app, "GET", "/probe", headers=duplicated).status_code == 403
    assert seen == ["CUST-A", "CUST-B"]


def test_context_header_is_ignored_in_single_mode(trust, identity):
    identity.bindings["U-1"] = "CUST-A"
    identity.grants["U-1"] = frozenset({VIEW_CUSTOMER})
    app, seen = probe_app(trust)
    response = call(app, "GET", "/probe", headers={**bearer(trust.mint()), "X-RFQ-Customer-Context": "CUST-B"})  # fmt: skip
    assert response.json() == {"customer": "CUST-A"}  # the binding decides, not the header


def test_selectable_context_with_one_binding_needs_no_header(trust, multi):
    multi["U-1"] = ("CUST-A",)
    app, _ = probe_app(trust, customer_context="selectable")
    assert call(app, "GET", "/probe", headers=bearer(trust.mint())).json() == {"customer": "CUST-A"}


def test_principal_assignment_scope():
    assert Principal("CUST-A", user_id="U-1").assigned_user is None
    assert Principal("CUST-A", user_id="U-1", assignment_required=True).assigned_user == "U-1"
    with pytest.raises(ValueError):  # enforcement without a verified user fails closed
        Principal("CUST-A", assignment_required=True).assigned_user  # noqa: B018


def test_real_principal_carries_user_and_assignment_flag(trust, identity):
    from fastapi import Depends

    from rfq_copilot.api.auth import get_principal

    identity.bindings["U-1"] = "CUST-A"
    app = api(trust, rfq_assignments="required")
    seen = []

    @app.get("/probe")
    async def probe(principal: Principal = Depends(get_principal)):  # noqa: B008
        seen.append((principal.user_id, principal.assigned_user))
        return {}

    call(app, "GET", "/probe", headers=bearer(trust.mint()))
    assert seen == [("U-1", "U-1")]


# --- TD-024: revocation wiring --------------------------------------------------------------


def test_revoked_token_is_unauthenticated_and_checked_only_when_enforced(trust, identity, monkeypatch):  # fmt: skip
    granted(identity, VIEW_CUSTOMER)
    calls = []

    async def revoked(session, user, digest, issued_at):
        calls.append((len(digest), type(issued_at)))
        return True

    monkeypatch.setattr(repositories, "token_revoked", revoked)
    token = trust.mint()
    off = api(trust)
    assert call(off, "GET", "/api/v1/customers/me", headers=bearer(token)).status_code != 401
    assert calls == []
    enforced = api(trust, token_revocation="enforced")
    response = call(enforced, "GET", "/api/v1/customers/me", headers=bearer(token))
    assert response.status_code == 401 and response.json() == {"detail": "Authentication required."}
    assert calls == [(64, int)]  # a SHA-256 hex digest, never the token itself


def test_unknown_revocation_status_fails_closed(trust, identity, monkeypatch):
    from sqlalchemy.exc import ProgrammingError

    granted(identity, VIEW_CUSTOMER)

    async def missing_table(session, user, digest, issued_at):
        raise ProgrammingError("SELECT", {}, Exception("relation does not exist"))

    monkeypatch.setattr(repositories, "token_revoked", missing_table)
    app = api(trust, token_revocation="enforced")
    for method, path in PROTECTED:
        response = call(app, method, path, headers=bearer(trust.mint()))
        assert response.status_code == 503 and "relation" not in response.text


def test_verify_returns_subject_and_integer_iat(trust):
    verified = trust.verifier().verify(trust.mint())
    assert verified.subject == SUBJECT and type(verified.issued_at) is int


# --- Upload idempotency key handling --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", " padded", "x" * 257, "tab\there", "ünicode"])
async def test_invalid_idempotency_key_is_rejected_before_storing(
    tmp_path, fakes, monkeypatch, key
):
    from rfq_copilot.api.auth import get_principal

    app = make_app(tmp_path)
    app.state.settings = app.state.settings.model_copy(update={"upload_idempotency": "enabled"})
    app.dependency_overrides[get_principal] = lambda: Principal("CUST-A", frozenset({CREATE_RFQ}), user_id="U-1")  # fmt: skip
    response = await upload(app, files=pdf_file(), headers={"Idempotency-Key": key.encode("utf-8")})
    assert response.status_code == 422 and response.json()["reason"] == "idempotency_key_invalid"
    assert stored(tmp_path) == []


@pytest.mark.asyncio
async def test_idempotency_key_without_verified_actor_fails_closed(tmp_path, fakes):
    app = make_app(tmp_path)  # injected Principal has no user_id
    app.state.settings = app.state.settings.model_copy(update={"upload_idempotency": "enabled"})
    response = await upload(app, files=pdf_file(), headers={"Idempotency-Key": "k-1"})
    assert response.status_code == 503 and stored(tmp_path) == []


@pytest.mark.asyncio
async def test_idempotency_header_is_ignored_when_off(tmp_path, fakes):
    app = make_app(tmp_path)
    response = await upload(app, files=pdf_file(), headers={"Idempotency-Key": ""})
    assert response.status_code == 201  # off: the header has no meaning


# --- TD-026: deep immutability --------------------------------------------------------------


def test_validated_request_cannot_be_mutated_after_validation():
    from test_api_schemas import submission as day17_submission

    request = RfqSubmissionRequest.model_validate(day17_submission())
    assert isinstance(request.parts, tuple)
    with pytest.raises(AttributeError):
        request.parts.append(request.parts[0])  # would bypass max_length and uniqueness
    with pytest.raises(TypeError):
        request.parts[0] = request.parts[0]
    with pytest.raises(ValidationError):
        request.parts = ()  # frozen model
    dumped = request.model_dump(mode="json")
    assert isinstance(dumped["parts"], list)
    assert RfqSubmissionRequest.model_validate(dumped) == request  # round trip unchanged


def test_request_lists_still_report_list_errors():
    from test_api_schemas import submission as day17_submission

    bad = {**day17_submission(), "parts": "not-a-list"}
    with pytest.raises(ValidationError) as caught:
        RfqSubmissionRequest.model_validate(bad)
    assert caught.value.errors()[0]["type"] == "list_type"


def test_response_models_hold_tuples_and_serialize_lists():
    page = MaterialPage(items=[MaterialSummary(material_id="M-1", category="c", grade="g")], limit=1, offset=0, has_more=False)  # fmt: skip
    assert isinstance(page.items, tuple)
    with pytest.raises(AttributeError):
        page.items.append(page.items[0])
    assert json.loads(page.model_dump_json())["items"] == [{"material_id": "M-1", "category": "c", "grade": "g"}]  # fmt: skip


# --- Missing-file listing and operator CLI contract ---------------------------------------


def test_missing_attachment_ids_lists_only_server_ids(tmp_path):
    folder = storage(tmp_path)
    present = stored_file(folder, 7200)
    gone = "ATT-" + "A" * 32 + ".pdf"
    ids = reconciler.missing_attachment_ids(folder, [present, gone, "not-a-server-name.pdf"])
    assert ids == ("ATT-" + "A" * 32,)
    assert os.listdir(folder) == [present]  # report only: nothing created or removed


@pytest.mark.parametrize(
    "argv",
    [
        ["grant", "--user", "U", "--customer", "C", "--operator", "O", "--reason", "because"],
        ["grant", "--user", "U", "--operator", "O", "--reason", "onboarding"],
        ["revoke-token", "--user", "U", "--operator", "O", "--reason", "security"],
        [
            "revoke-token",
            "--user",
            "U",
            "--operator",
            "O",
            "--reason",
            "security",
            "--token-stdin",
            "--all-before-now",
        ],  # fmt: skip
    ],
)
def test_identity_cli_rejects_incomplete_or_free_text_arguments(argv):
    with pytest.raises(SystemExit):
        identity_admin.main(argv)


def test_identity_cli_reports_fixed_refusal_codes(monkeypatch, capsys):
    async def refused(args):
        raise identity_admin.Refused("operator_not_authorized")

    monkeypatch.setattr(identity_admin, "_run", refused)
    code = identity_admin.main(["grant", "--user", "U-1", "--customer", "CUST-A", "--operator", "U-9", "--reason", "onboarding"])  # fmt: skip
    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "refused",
        "error": "operator_not_authorized",
    }


def test_database_free_api_still_imports_no_admin_or_proposed_modules(tmp_path):
    import subprocess
    import sys

    probe = "import sys, rfq_copilot.api.app; print(sorted(m for m in ('rfq_copilot.identity_admin', 'rfq_copilot.seeding.policy', 'sqlalchemy') if m in sys.modules))"  # fmt: skip
    env = {k: v for k, v in os.environ.items() if "DATABASE" not in k}
    out = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)  # fmt: skip
    assert out.stdout.strip() == "[]", out.stderr


def test_fake_database_contract_still_counts_sessions():
    assert FakeDatabase().opened == 0


# --- D1-D3: pinned permission-policy overlay and the identity_operator role -----------------


def test_policy_file_matches_its_pin_and_contains_exactly_the_approved_rows():
    import hashlib

    from rfq_copilot.seeding.policy import POLICIES_DIR, TRUSTED_POLICIES, policy_rows

    name, pinned = TRUSTED_POLICIES["rbac-policy-v2"]
    assert hashlib.sha256((POLICIES_DIR / name).read_bytes()).hexdigest() == pinned
    rows = policy_rows("rbac-policy-v2")
    assert rows["roles"] == ({"role": "identity_operator"},)
    grants = {(g["role"], g["permission"]) for g in rows["role_permissions"]}
    assert all(g["allowed"] is True for g in rows["role_permissions"])
    assert {(r, p) for r, p in grants if p in {"manage_customer_bindings", "revoke_tokens"}} == {
        ("identity_operator", "manage_customer_bindings"),
        ("identity_operator", "revoke_tokens"),
    }
    assert not {r for r, _ in grants} & {"administrator"}  # no automatic admin authority
    assert {p for r, p in grants if r == "auditor"} == {"view_quote"}
    v2 = {ROUTE_PERMISSIONS["v2"][op] for op in ("catalog_read", "quote_read", "attachment_upload")}
    assert v2 <= {p["permission"] for p in rows["permissions"]}


def test_overlay_state_is_all_or_nothing():
    from rfq_copilot import schema_contract
    from rfq_copilot.seeding import policy

    rows = policy.policy_rows("rbac-policy-v2")
    full = {t: list(rows[t]) for t in policy.OVERLAY_TABLES}
    original = schema_contract._ACTIVE
    try:
        schema_contract._ACTIVE = schema_contract.CURRENT
        assert policy.overlay_state({}) == "absent"
        assert policy.overlay_state(full) == "applied:rbac-policy-v2"
        partial = {**full, "role_permissions": full["role_permissions"][:-1]}
        assert policy.overlay_state(partial) is None
        modified = {**full, "role_permissions": [{**full["role_permissions"][0], "allowed": False}, *full["role_permissions"][1:]]}  # fmt: skip
        assert policy.overlay_state(modified) is None
        assert policy.is_policy_extra("roles", {"role": "identity_operator"})
        assert not policy.is_policy_extra("role_permissions", {"role": "administrator", "permission": "revoke_tokens", "allowed": True})  # fmt: skip
        schema_contract._ACTIVE = schema_contract.PREVIOUS  # a contract without overlays
        assert not policy.is_policy_extra("roles", {"role": "identity_operator"})
        assert policy.overlay_state(full) is None
    finally:
        schema_contract._ACTIVE = original


def test_active_contract_is_the_activated_one():
    from rfq_copilot import schema_contract
    from rfq_copilot.models import Role
    from rfq_copilot.seeding import safety

    current = schema_contract.CURRENT
    assert schema_contract.active() is current and current.revision == "20261111_06"
    assert (safety.REVISION, safety.SHAPE_DIGEST) == (current.revision, current.shape_digest)
    assert current.policy_overlays == frozenset({"rbac-policy-v2"})
    assert schema_contract.PREVIOUS.policy_overlays == frozenset()
    role_check = next(str(c.sqltext) for c in Role.__table__.constraints if "role IN" in str(getattr(c, "sqltext", "")))  # fmt: skip
    assert "'identity_operator'" in role_check


def test_tampered_policy_file_is_refused(tmp_path, monkeypatch):
    import shutil

    from rfq_copilot.seeding import SeedError, policy

    copy = tmp_path / "policies"
    shutil.copytree(policy.POLICIES_DIR, copy)
    path = copy / "rbac-policy-v2.json"
    path.write_text(path.read_text().replace('"identity_operator", "revoke_tokens"', '"administrator", "revoke_tokens"'))  # fmt: skip
    monkeypatch.setattr(policy, "POLICIES_DIR", copy)
    with pytest.raises(SeedError):
        policy.policy_rows("rbac-policy-v2")


# --- Final Phase 3.5 tasks: CLI contracts and the NEXT contract ------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [
            "provision",
            "--new-user",
            "U",
            "--display-name",
            "N",
            "--email",
            "e@x",
            "--region",
            "R",
            "--subject",
            "s",
            "--verification-ref",
            "SEC-1",
            "--approved-by",
            "A",
        ],  # neither requester nor bootstrap  # fmt: skip
        [
            "provision",
            "--new-user",
            "U",
            "--display-name",
            "N",
            "--email",
            "e@x",
            "--region",
            "R",
            "--subject",
            "s",
            "--verification-ref",
            "SEC-1",
            "--approved-by",
            "A",
            "--requested-by",
            "O",
            "--bootstrap",
        ],  # fmt: skip
        ["deactivate", "--operator-user", "U", "--approved-by", "A", "--reason", "because"],
    ],
)
def test_operator_cli_rejects_incomplete_or_conflicting_arguments(argv):
    from rfq_copilot import operator_admin

    with pytest.raises(SystemExit):
        operator_admin.main(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["grant", "--user", "U", "--rfq", "R", "--operator", "O", "--reason", "free text"],
        ["grant", "--user", "U", "--operator", "O", "--reason", "onboarding"],
        ["list"],
        ["list", "--user", "U", "--rfq", "R"],
    ],
)
def test_assignment_cli_rejects_incomplete_arguments(argv):
    from rfq_copilot import assignment_admin

    with pytest.raises(SystemExit):
        assignment_admin.main(argv)


def test_admin_clis_report_fixed_refusal_codes(monkeypatch, capsys):
    from rfq_copilot import assignment_admin, operator_admin
    from rfq_copilot.identity_admin import Refused

    async def refused(args):
        raise Refused("operator_not_authorized")

    for module, argv in (
        (
            assignment_admin,
            ["grant", "--user", "U", "--rfq", "R", "--operator", "O", "--reason", "onboarding"],
        ),  # fmt: skip
        (
            operator_admin,
            ["deactivate", "--operator-user", "U", "--approved-by", "A", "--reason", "offboarding"],
        ),  # fmt: skip
    ):
        monkeypatch.setattr(module, "_run", refused)
        assert module.main(argv) == 2
        assert json.loads(capsys.readouterr().out) == {"status": "refused", "error": "operator_not_authorized"}  # fmt: skip


def test_next_contract_drops_only_the_one_active_binding_index():
    from rfq_copilot import schema_contract
    from rfq_copilot.models import Base

    assert schema_contract.active() is schema_contract.CURRENT  # _05 is not active
    nxt = schema_contract.NEXT
    assert nxt.revision == "20261111_05" and nxt.multi_customer_bindings
    current = {i.name for t in Base.metadata.tables.values() for i in t.indexes}
    following = {i.name for t in nxt.metadata().tables.values() for i in t.indexes}
    assert current - following == {"uq_user_customer_bindings_active_user"} and following <= current
    assert dict(nxt.catalog_counts) == dict(schema_contract.CURRENT.catalog_counts)
