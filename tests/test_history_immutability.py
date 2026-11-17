"""K2 (Day 54A) offline checks: revision chain, allowlist, runtime configuration, model.

No database connection: catalog behaviour is in ``test_history_immutability_database.py``
and runtime privileges in ``test_runtime_role_database.py``.
"""

import re
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql

from rfq_copilot import schema_contract
from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
from rfq_copilot.database import RUNTIME_INSERTABLE, RUNTIME_UPDATABLE
from rfq_copilot.models import QuoteVersion
from rfq_copilot.models.quotations import Xid8
from rfq_copilot.seeding.mapping import DATABASE_STAMPED
from rfq_copilot.seeding.safety import trigger_type

ROOT = Path(__file__).resolve().parents[1]
URL = "postgresql+asyncpg://{role}:k2-secret@127.0.0.1:5432/{database}"


def environment(**changes):
    values = {
        "DATABASE_URL": URL.format(role="owner_role", database="example_dev"),
        "TEST_DATABASE_URL": URL.format(role="owner_role", database="example_test"),
        "TEST_DATABASE_NAME": "example_test",
        "APP_DATABASE_URL": URL.format(role="app_role", database="example_dev"),
        "TEST_APP_DATABASE_URL": URL.format(role="app_test_role", database="example_test"),
        **changes,
    }
    return {key: value for key, value in values.items() if value is not None}


def test_k3_is_the_single_active_head_and_05_is_merged_without_rewriting_history():
    active = ScriptDirectory.from_config(schema_contract.CURRENT.alembic_config())
    assert active.get_heads() == ["20261116_04"] == [schema_contract.CURRENT.revision]
    assert active.get_revision("20261116_04").down_revision == "20261116_03"  # K5 on K4
    assert active.get_revision("20261116_03").down_revision == "20261116_01"
    assert active.get_revision("20261116_01").down_revision == "20261115_01"
    assert active.get_revision("20261115_01").down_revision == "20261111_06"
    following = ScriptDirectory.from_config(schema_contract.NEXT.alembic_config())
    assert following.get_heads() == ["20261116_02"] == [schema_contract.NEXT.revision]
    # 05 keeps its original parent (Day 54B R4); the schema-free merge joins it with K3.
    assert following.get_revision("20261111_05").down_revision == "20261111_06"
    assert following.get_revision("20261116_02").down_revision == ("20261111_05", "20261116_04")


def test_the_allowlist_is_the_revision_itself():
    functions, triggers = schema_contract.history_guards(schema_contract.K2.guards_revisions[0])
    assert set(functions) == {
        "k2_refuse_change",
        "k2_quote_versions_guard",
        "k2_quote_line_items_guard",
        "k2_quotes_guard",
    }
    by_table = {table: (name, function, events) for name, table, function, events in triggers}
    assert len(by_table) == len(triggers) == 10
    for table in ("quote_versions", "quote_line_items", "quotes"):
        assert by_table[table][2] == ("INSERT", "UPDATE", "DELETE")
    append_only = {"audit_events", "idempotency_records", "approval_history", "approval_requests",
                   "pricing_calculations", "rfqs", "rfq_line_items"}  # fmt: skip
    assert {t for t, (_, f, _) in by_table.items() if f == "k2_refuse_change"} == append_only
    assert all(by_table[t][2] == ("UPDATE", "DELETE") for t in append_only)
    # Approved or released rows can be inserted only by the table owner (seeding of history).
    for name in ("k2_quote_versions_guard", "k2_quotes_guard"):
        assert "pg_has_role(current_user" in functions[name] and "TG_RELID" in functions[name]
    assert "NEW.status = 'draft'" in functions["k2_quotes_guard"]
    for body in functions.values():  # refusals name the trigger; no dynamic SQL, no bypass flag
        assert "CONSTRAINT = TG_NAME" in body and "integrity_constraint_violation" in body
        assert "EXECUTE " not in body and "current_setting" not in body
    assert "pg_current_xact_id()" in functions["k2_quote_versions_guard"]
    assert "created_xact = pg_current_xact_id()" in functions["k2_quote_line_items_guard"]


def test_the_allowlist_is_pinned_independently_of_the_revision_file(monkeypatch, tmp_path):
    revision = schema_contract.K2.guards_revisions[0]
    assert schema_contract.GUARD_FILE_SHA256[revision] == (
        "10021900678979e4ce1c1d48c64fbcafa8251fc724c11a96659c702ab5bf082d"
    )  # K2's pin is preserved by K3
    edited = tmp_path / f"{revision}_history_immutability.py"
    (original,) = schema_contract.VERSIONS.glob(f"{revision}_*.py")
    edited.write_bytes(original.read_bytes().replace(b"'A'", b"'O'") + b"\n# edited\n")
    monkeypatch.setattr(schema_contract, "VERSIONS", tmp_path)
    schema_contract.history_guards.cache_clear()
    try:
        with pytest.raises(ValueError, match="pinned SHA-256"):
            schema_contract.history_guards(revision)
    finally:
        monkeypatch.undo()
        schema_contract.history_guards.cache_clear()


def test_trigger_disabling_helpers_never_reach_application_code():
    for path in (ROOT / "src").rglob("*.py"):
        source = path.read_text()
        assert "history_corruption" not in source, path
        assert "DISABLE TRIGGER" not in source.upper(), path
        assert "SESSION_REPLICATION_ROLE" not in source.upper() or path.name == "database.py"
    guarded = (
        "20261115_01_history_immutability.py",
        "20261116_01_k3_calculation_provenance.py",
        "20261116_03_k4_approval_lifecycle.py",
        "20261116_04_k5_decision_authority.py",
    )
    for name in guarded:
        migration = (ROOT / "alembic" / "versions" / name).read_text()
        assert "ENABLE ALWAYS TRIGGER" in migration and "SECURITY DEFINER" not in migration


def test_trigger_types_are_before_row_or_deferred_after_row_triggers():
    assert trigger_type(("INSERT", "UPDATE", "DELETE")) == 1 | 2 | 4 | 8 | 16
    assert trigger_type(("UPDATE", "DELETE")) == 1 | 2 | 8 | 16
    assert trigger_type(("INSERT",), "deferred") == 1 | 4  # AFTER: no BEFORE bit


def test_contracts_without_a_guard_revision_allow_no_routines():
    assert schema_contract.PREVIOUS.guards_revisions == ()
    assert schema_contract.ACTIVATED_06.guards_revisions == ()
    assert schema_contract.K2.guards_revisions == ("20261115_01",)
    assert schema_contract.K3.guards_revisions == ("20261115_01", "20261116_01")
    assert schema_contract.K4.guards_revisions == ("20261115_01", "20261116_01", "20261116_03")
    assert schema_contract.CURRENT.guards_revisions == (*schema_contract.K4.guards_revisions, "20261116_04")  # fmt: skip
    assert schema_contract.NEXT.guards_revisions == schema_contract.CURRENT.guards_revisions


def test_the_runtime_configuration_is_a_different_role_on_the_same_database():
    owner = DatabaseConfig.from_env("development", environ=environment())
    runtime = DatabaseConfig.from_env("development", environ=environment(), runtime=True)
    assert (owner.url.username, owner.runtime) == ("owner_role", False)
    assert (runtime.url.username, runtime.runtime, runtime.url.database) == ("app_role", True, "example_dev")  # fmt: skip
    testing = DatabaseConfig.from_env("test", environ=environment(), runtime=True)
    assert (testing.url.username, testing.url.database) == ("app_test_role", "example_test")
    assert "k2-secret" not in repr(runtime) and "k2-secret" not in str(runtime)


@pytest.mark.parametrize(
    "target,changes",
    [("development", {"APP_DATABASE_URL": None}),
     ("test", {"TEST_APP_DATABASE_URL": None}),
     ("development", {"APP_DATABASE_URL": URL.format(role="owner_role", database="example_dev")}),
     ("test", {"TEST_APP_DATABASE_URL": URL.format(role="owner_role", database="example_test")}),
     ("development", {"APP_DATABASE_URL": URL.format(role="app_role", database="example_test")}),
     ("test", {"TEST_APP_DATABASE_URL": URL.format(role="app_test_role", database="example_dev")}),
     ("development", {"APP_DATABASE_URL": URL.format(role="app_role", database="example_dev").replace("5432", "5433")}),
     ("development", {"DATABASE_URL": None})],
)  # fmt: skip
def test_the_runtime_configuration_fails_closed_and_never_falls_back(target, changes):
    with pytest.raises(DatabaseConfigurationError) as caught:
        DatabaseConfig.from_env(target, environ=environment(**changes), runtime=True)
    assert "k2-secret" not in str(caught.value) and "postgresql" not in str(caught.value)


def test_the_updatable_allowlist_is_the_provisioning_artifact():
    sql = (ROOT / "docs" / "database" / "k2_runtime_roles.sql").read_text()
    updates = re.findall(r"^GRANT UPDATE \(([^)]+)\) ON business\.(\w+)", sql, re.M)
    granted = {f"{t}.{column.strip()}" for columns, t in updates for column in columns.split(",")}
    assert granted == RUNTIME_UPDATABLE
    inserts = re.findall(r"^GRANT INSERT ON (.+?) TO ", sql, re.M)
    insertable = {t.strip().removeprefix("business.") for line in inserts for t in line.split(",")}
    # K2 tables + K3 pricing_calculations + K4 approval rows
    assert insertable == RUNTIME_INSERTABLE
    assert "business_rule_versions" not in insertable and "rfqs" not in insertable
    privileges = re.findall(r"^GRANT (.+?) ON ", sql, re.M)
    assert {p.split(" ")[0] for p in privileges} == {"CONNECT", "USAGE", "SELECT", "INSERT", "UPDATE"}  # fmt: skip
    assert all("(" in p for p in privileges if p.startswith("UPDATE"))  # columns only
    assert not re.search(r"^GRANT\s+\S+\s+TO\b", sql, re.M)  # no role membership
    assert not re.search(r"PASSWORD\s+'", sql, re.I)  # set interactively, never in a file


def test_the_creation_seal_is_database_stamped_and_never_application_data():
    column = QuoteVersion.__table__.c.created_xact
    assert isinstance(column.type, Xid8) and column.nullable
    assert column.default is None and column.server_default is None
    assert column.type.compile(dialect=postgresql.dialect()) == "XID8"
    assert "created_xact" in DATABASE_STAMPED
    assert QuoteVersion(quote_id="Q", version=1).created_xact is None  # never set by the app
    assert postgresql.base.ischema_names["xid8"] is Xid8  # reflected as itself, not NullType
