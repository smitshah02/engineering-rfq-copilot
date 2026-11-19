"""Customer role, rbac-policy-v3, route mapping, contract and dev tokens (no database)."""

import importlib.util
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.util import CommandError
from test_auth import AUDIENCE, ISSUER, pem_public, write_key

from rfq_copilot import dev_token, operator_admin, schema_contract
from rfq_copilot.api import auth
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.tokens import TokenVerifier
from rfq_copilot.mcp.contracts import TOOLS
from rfq_copilot.models import Role
from rfq_copilot.quote_approval import APPROVE, REJECT, SUBMIT
from rfq_copilot.quote_drafts import OPERATION as CREATE_QUOTE_DRAFT
from rfq_copilot.seeding.policy import TRUSTED_POLICIES, policy_rows

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "20261117_01_customer_role.py"
STAFF_AND_ADMIN = {
    CREATE_QUOTE_DRAFT, SUBMIT, APPROVE, REJECT, "release_quote", "edit_quote_draft",
    "create_rfq", "view_assigned_rfqs", "view_catalog", "view_quote", "attach_rfq_document",
    "manage_customer_bindings", "revoke_tokens",
}  # fmt: skip


class _Op:
    """Records alembic ``op`` calls; ``get_bind`` answers the in-use probe."""

    def __init__(self, in_use):
        self.calls, self.in_use = [], in_use

    def f(self, name):
        return name

    def drop_constraint(self, *args, **kwargs):
        self.calls.append(("drop_constraint", args, kwargs))

    def create_check_constraint(self, *args, **kwargs):
        self.calls.append(("create_check_constraint", args, kwargs))

    def get_bind(self):
        return SimpleNamespace(execute=lambda _: SimpleNamespace(scalar=lambda: self.in_use))


def _record(module, *, in_use, step):
    recorder = _Op(in_use)
    module.op = recorder  # a freshly loaded module: nothing else sees the stand-in
    getattr(module, step)()
    return recorder.calls


def _roles(sql):
    return set(re.findall(r"'([a-z_]+)'", sql))


def _model_roles_check():
    (check,) = [
        c for c in Role.__table__.constraints if "role IN" in str(getattr(c, "sqltext", ""))
    ]
    return check


def customer_grants(version="rbac-policy-v3"):
    return {r["permission"] for r in policy_rows(version)["role_permissions"] if r["role"] == "customer"}  # fmt: skip


# --- 6, 10: least privilege and policy integrity ----------------------------------------------


def test_v3_is_pinned_and_is_v2_plus_only_the_customer_role():
    assert set(TRUSTED_POLICIES) == {"rbac-policy-v2", "rbac-policy-v3", "rbac-policy-v4"}  # A2: v4
    v2, v3 = policy_rows("rbac-policy-v2"), policy_rows("rbac-policy-v3")
    for table in v2:
        assert all(row in v3[table] for row in v2[table])  # a strict superset (upgrade path)
    added = [r for r in v3["role_permissions"] if r not in v2["role_permissions"]]
    assert {r["role"] for r in added} == {"customer"}
    assert customer_grants() == operator_admin.CUSTOMER_PERMISSIONS == {
        "view_customer", "customer_view_rfq", "customer_create_rfq", "customer_view_quote",
        "customer_attach_rfq_document",
    }  # fmt: skip


def test_customers_hold_no_staff_costing_mcp_decision_or_admin_permission():
    assert not customer_grants() & STAFF_AND_ADMIN
    assert not customer_grants() & {tool.permission for tool in TOOLS}  # no MCP tool at all
    # view_customer authorizes only the profile route; nothing else in any policy table.
    assert {op for table in auth.ROUTE_PERMISSIONS.values() for op, p in table.items() if p == auth.VIEW_CUSTOMER} == {"customer_profile"}  # fmt: skip


def test_route_mapping_v1_v2_unchanged_and_customer_permissions_only_in_v3():
    for policy in ("v1", "v2"):
        for operation, permission in auth.ROUTE_PERMISSIONS[policy].items():
            assert auth.accepted_permissions(policy, operation) == {permission}
    assert auth.ROUTE_PERMISSIONS["v3"] == auth.ROUTE_PERMISSIONS["v2"]
    accepted = {op: auth.accepted_permissions("v3", op) - {auth.ROUTE_PERMISSIONS["v3"][op]} for op in auth.ROUTE_PERMISSIONS["v3"]}  # fmt: skip
    assert accepted == {
        "customer_profile": set(),  # customers hold view_customer itself
        "rfq_read": {auth.CUSTOMER_VIEW_RFQ},
        "attachment_list": {auth.CUSTOMER_VIEW_RFQ},
        "catalog_read": set(),  # never customer-visible
        "quote_read": {auth.CUSTOMER_VIEW_QUOTE},
        "attachment_upload": {auth.CUSTOMER_ATTACH_RFQ_DOCUMENT},
    }
    assert set(auth.CUSTOMER_ROUTE_PERMISSIONS.values()) <= customer_grants()


def test_v3_cannot_be_the_hardened_profile():
    with pytest.raises(ValueError):
        ApiSettings(deployment_profile="hardened", permission_policy="v3")


# --- 10: migration and schema contract ---------------------------------------------------------


def test_customer_revision_is_the_head_on_k5_and_keeps_every_pin():
    spec = importlib.util.spec_from_file_location("customer_role", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("20261117_01", "20261116_04")
    # Behaviour, recorded through a stand-in for alembic's op: upgrade swaps only the roles
    # CHECK; downgrade restores the K5 list, or refuses while the role is in use.
    upgrade_ops = _record(module, in_use=False, step="upgrade")
    assert [name for name, *_ in upgrade_ops] == ["drop_constraint", "create_check_constraint"]
    assert all(args[:2] == ("ck_roles_rule_01", "roles") for _, args, _ in upgrade_ops)
    widened = _roles(upgrade_ops[1][1][2])
    # The model also admits A2's rfq_workflow_agent (20261117_02), on top of this revision.
    assert widened == _roles(str(_model_roles_check().sqltext)) - {"rfq_workflow_agent"} and "customer" in widened  # fmt: skip
    assert _roles(_record(module, in_use=False, step="downgrade")[1][1][2]) == widened - {"customer"}  # fmt: skip
    with pytest.raises(CommandError):
        _record(module, in_use=True, step="downgrade")
    assert not hasattr(module, "TRIGGERS") and not hasattr(module, "FUNCTIONS")  # no guard
    assert "identity_operator" in widened
    current, k5 = schema_contract.CUSTOMER, schema_contract.K5  # the record; A1/A2 is CURRENT
    assert schema_contract.active() is schema_contract.CURRENT and current.revision == "20261117_01"
    assert k5.revision == "20261116_04"
    assert k5.shape_digest == "9f84633742a079c0bc047ce907c4f92ea6a700cffde54dba6d326abf35dcd259"
    assert (current.shape_digest, dict(current.catalog_counts)) == (k5.shape_digest, dict(k5.catalog_counts))  # fmt: skip
    assert current.guards_revisions == k5.guards_revisions
    assert current.policy_overlays == {"rbac-policy-v2", "rbac-policy-v3"}
    assert "20261117_01" not in schema_contract.GUARD_FILE_SHA256


# --- D: development tokens ---------------------------------------------------------------------


@pytest.fixture
def dev_keys(tmp_path):
    private_path, public_path = dev_token.keygen(tmp_path / "keys")
    return private_path, public_path


def dev_settings(public_path, **overrides):
    values = {"environment": "development", "jwt_issuer": ISSUER, "jwt_audience": AUDIENCE,
              "jwt_algorithm": "EdDSA", "jwt_public_key_file": public_path, **overrides}  # fmt: skip
    return ApiSettings(**values)


def test_keygen_restricts_permissions_and_never_overwrites(dev_keys):
    private_path, public_path = dev_keys
    assert private_path.stat().st_mode & 0o777 == 0o600
    assert private_path.parent.stat().st_mode & 0o777 == 0o700
    assert public_path.stat().st_mode & 0o777 == 0o644
    with pytest.raises(dev_token.DevTokenError):
        dev_token.keygen(private_path.parent)
    assert not (ROOT / dev_token.PRIVATE_NAME).exists()  # nothing written into the repository


def test_issued_token_passes_the_api_verifier_with_exact_claims(dev_keys):
    private_path, public_path = dev_keys
    settings = dev_settings(public_path)
    token = dev_token.issue(settings, private_path, "auth|cust-1", 600)
    verified = TokenVerifier.from_settings(settings).verify(token)
    assert verified.subject == "auth|cust-1"


@pytest.mark.parametrize(
    ("change", "ttl"),
    [({"environment": "production"}, 600), ({}, 10_000), ({}, 30)],
    ids=["production", "too-long", "too-short"],
)
def test_issue_refuses_production_and_out_of_range_lifetimes(dev_keys, change, ttl):
    private_path, public_path = dev_keys
    with pytest.raises(dev_token.DevTokenError):
        dev_token.issue(dev_settings(public_path, **change), private_path, "auth|cust-1", ttl)


def test_issue_refuses_a_readable_private_key_and_a_mismatched_pair(dev_keys, tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private_path, public_path = dev_keys
    os.chmod(private_path, 0o644)
    with pytest.raises(dev_token.DevTokenError):
        dev_token.issue(dev_settings(public_path), private_path, "auth|cust-1", 600)
    os.chmod(private_path, 0o600)
    stranger = write_key(tmp_path, "other.pub.pem", pem_public(ed25519.Ed25519PrivateKey.generate()), 0o644)  # fmt: skip
    with pytest.raises(dev_token.DevTokenError):  # self-check against the configured verifier
        dev_token.issue(dev_settings(stranger), private_path, "auth|cust-1", 600)


def test_no_token_issuing_http_route_exists(dev_keys):
    from test_backend_integration import DOCS, _walk

    from rfq_copilot.api.app import create_app

    _, public_path = dev_keys
    operations, other = set(), []  # every route, nested included routers too
    _walk(create_app(dev_settings(public_path)).routes, "", operations, other)
    assert operations and set(other) <= DOCS  # non-API routes are only the docs pages
    paths = {path.lower() for _, path in operations}
    assert not [p for p in paths if any(w in p for w in ("token", "login", "auth", "jwt"))]
