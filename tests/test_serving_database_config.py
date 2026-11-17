"""Owner-free serving configuration and fail-closed factories; no PostgreSQL required."""

from collections.abc import Mapping
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from serving_process import mcp_process_environment
from sqlalchemy.engine import URL

from rfq_copilot import database as db
from rfq_copilot.api import db as api_db
from rfq_copilot.api.app import create_app
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.decision_app import create_decision_app
from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
from rfq_copilot.mcp import host


# Synthetic credentials only. Authority is proved by live privilege checks, not role names.
def serving_environment(*, decision=False, target="development"):
    variable = "DECISION_DATABASE_URL" if decision else "APP_DATABASE_URL"
    name = "rfq_copilot_dev"
    if target == "test":
        variable = "TEST_" + variable
        name = "rfq_copilot_test"
    return {
        variable: URL.create(
            "postgresql+asyncpg",
            username="synthetic_decision" if decision else "synthetic_app",
            password="synthetic-serving-password",
            host="127.0.0.1",
            port=5432,
            database=name,
        ).render_as_string(hide_password=False),
        "TEST_DATABASE_NAME": "rfq_copilot_test",
    }


class SelectedKeysOnly(Mapping):
    """Reject enumeration or access to owner/other-role credentials, even if configured."""

    def __init__(self, values):
        self.values = values
        self.read = set()

    def __getitem__(self, key):
        self.read.add(key)
        return self.values[key]

    def __iter__(self):
        raise AssertionError("Serving configuration must not enumerate the environment")

    def __len__(self):
        raise AssertionError("Serving configuration must not copy the environment")


@pytest.mark.parametrize("decision", [False, True])
@pytest.mark.parametrize("target", ["development", "test"])
def test_serving_reads_only_one_role_and_nonsecret_metadata(decision, target):
    env = SelectedKeysOnly(serving_environment(decision=decision, target=target))
    config = DatabaseConfig.from_serving_env(target, environ=env, decision=decision)
    assert config.runtime is not decision and config.decision is decision
    selected = next(k for k in env.values if k.endswith("DATABASE_URL"))
    assert env.read <= {
        selected,
        "DATABASE_HOST",
        "DATABASE_PORT",
        "DEVELOPMENT_DATABASE_NAME",
        "TEST_DATABASE_NAME",
    }
    assert "synthetic-serving-password" not in repr(config)


@pytest.mark.parametrize("decision", [False, True])
def test_owner_credentials_neither_required_nor_validated(decision, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DATABASE_URL=must-not-be-discovered\n")
    monkeypatch.chdir(tmp_path)
    env = serving_environment(decision=decision)
    env.update(DATABASE_URL="malformed-owner", TEST_DATABASE_URL="malformed-test-owner")
    env["APP_DATABASE_URL" if decision else "DECISION_DATABASE_URL"] = "other-role-secret"
    selected = SelectedKeysOnly(env)
    DatabaseConfig.from_serving_env(environ=selected, decision=decision)
    assert not {"DATABASE_URL", "TEST_DATABASE_URL"} & selected.read
    assert ("APP_DATABASE_URL" if decision else "DECISION_DATABASE_URL") not in selected.read


@pytest.mark.parametrize("decision", [False, True])
def test_no_owner_fallback_for_missing_restricted_url(decision):
    with pytest.raises(DatabaseConfigurationError, match="is required"):
        DatabaseConfig.from_serving_env(
            environ={"DATABASE_URL": "owner-must-never-be-used"}, decision=decision
        )


@pytest.mark.parametrize("decision", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        {"URL": "not-a-url"},
        {"URL": "postgresql://fake:fake@127.0.0.1/rfq_copilot_dev"},
        {"URL": "postgresql+asyncpg://fake@127.0.0.1/rfq_copilot_dev"},
        {"DATABASE_HOST": "other.invalid"},
        {"DATABASE_PORT": "5433"},
        {"DATABASE_PORT": "not-a-port"},
        {"DATABASE_PORT": "70000"},
        {"DEVELOPMENT_DATABASE_NAME": "another_database"},
    ],
)
def test_invalid_restricted_configuration_fails_without_values(decision, change):
    env = serving_environment(decision=decision)
    selected = "DECISION_DATABASE_URL" if decision else "APP_DATABASE_URL"
    env.update({selected if k == "URL" else k: v for k, v in change.items()})
    with pytest.raises(DatabaseConfigurationError) as caught:
        DatabaseConfig.from_serving_env(environ=env, decision=decision)
    assert "synthetic-serving-password" not in str(caught.value)
    assert "://" not in str(caught.value)


@pytest.mark.parametrize("decision", [False, True])
@pytest.mark.parametrize("name", [None, "rfq_copilot_dev", "wrong_test"])
def test_test_serving_keeps_explicit_database_guards(decision, name):
    env = serving_environment(decision=decision, target="test")
    if name is None:
        del env["TEST_DATABASE_NAME"]
    else:
        env["TEST_DATABASE_NAME"] = name
    with pytest.raises(DatabaseConfigurationError):
        DatabaseConfig.from_serving_env("test", environ=env, decision=decision)


def test_test_and_development_names_must_differ():
    env = serving_environment(target="test") | {"DEVELOPMENT_DATABASE_NAME": "rfq_copilot_test"}
    with pytest.raises(DatabaseConfigurationError, match="distinct"):
        DatabaseConfig.from_serving_env("test", environ=env)


def test_unknown_target_fails_before_credential_access():
    with pytest.raises(DatabaseConfigurationError):
        DatabaseConfig.from_serving_env("production", environ=SelectedKeysOnly({}))


def test_mcp_child_environment_excludes_owner_decision_and_unrelated_secrets():
    secrets = {
        "DATABASE_URL": "synthetic-owner",
        "TEST_DATABASE_URL": "synthetic-test-owner",
        "DECISION_DATABASE_URL": "synthetic-decision",
        "TEST_DECISION_DATABASE_URL": "synthetic-test-decision",
        "LANGFUSE_SECRET_KEY": "synthetic-telemetry",
        "APP_DATABASE_URL": "wrong-target-app",
    }
    selected = mcp_process_environment(
        {**secrets, "TEST_APP_DATABASE_URL": "file-app", "TEST_DATABASE_NAME": "example_test"},
        {**secrets, "PATH": "/synthetic/bin", "TEST_APP_DATABASE_URL": "process-app"},
    )
    assert selected == {
        "PATH": "/synthetic/bin",
        "TEST_APP_DATABASE_URL": "process-app",
        "TEST_DATABASE_NAME": "example_test",
    }


@pytest.fixture
def serving_process(monkeypatch):
    for key in (
        "APP_DATABASE_URL",
        "TEST_APP_DATABASE_URL",
        "DECISION_DATABASE_URL",
        "TEST_DECISION_DATABASE_URL",
        "DATABASE_HOST",
        "DATABASE_PORT",
        "DEVELOPMENT_DATABASE_NAME",
        "TEST_DATABASE_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in serving_environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "invalid-owner-is-irrelevant")
    return monkeypatch


class DatabaseSpy:
    def __init__(self, config, *, read_only=False):
        self.config = config
        self.read_only = read_only


@pytest.mark.asyncio
async def test_rest_factories_select_restricted_application_role(serving_process):
    serving_process.setattr(db, "Database", DatabaseSpy)
    app = create_app(ApiSettings())
    request = SimpleNamespace(app=app)
    read = await api_db.get_database(request)
    write = await api_db.get_write_database(request)
    assert read is not write and read.read_only and not write.read_only
    assert read.config.runtime and write.config.runtime
    assert not read.config.decision and not write.config.decision


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [api_db.get_database, api_db.get_write_database])
async def test_rest_never_falls_back_to_owner(serving_process, factory):
    serving_process.delenv("APP_DATABASE_URL")
    with pytest.raises(HTTPException) as caught:
        await factory(SimpleNamespace(app=create_app(ApiSettings())))
    assert caught.value.status_code == 503


def test_decision_factory_reads_its_role_only(serving_process):
    for key, value in serving_environment(decision=True).items():
        serving_process.setenv(key, value)
    serving_process.setenv("APP_DATABASE_URL", "invalid-app-is-irrelevant")
    serving_process.setattr(db, "Database", DatabaseSpy)
    app = create_decision_app(ApiSettings())
    assert app.state.database is app.state.decision_database
    assert app.state.database.config.decision and not app.state.database.config.runtime


def test_decision_factory_fails_without_its_credential(serving_process):
    with pytest.raises(DatabaseConfigurationError, match="is required"):
        create_decision_app(ApiSettings())


def test_mcp_factory_selects_only_application_role(serving_process):
    serving_process.setenv("DECISION_DATABASE_URL", "must-not-be-read")
    serving_process.setattr(host.McpAuthSettings, "from_env", lambda env: "auth")
    serving_process.setattr(host.McpHostSettings, "from_env", lambda env: "host")
    serving_process.setattr(host, "Database", DatabaseSpy)
    serving_process.setattr(host, "create_mcp_host", lambda h, a, database, **kw: database)
    database = host.create_app_from_env()
    assert database.config.runtime and not database.config.decision and database.read_only
    serving_process.delenv("APP_DATABASE_URL")
    with pytest.raises(DatabaseConfigurationError):
        host.create_app_from_env()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [False, True])
@pytest.mark.parametrize("refuse", [False, True])
async def test_restricted_sessions_validate_before_yield_and_clear_preflight(
    monkeypatch, decision, refuse
):
    calls = []

    class Session:
        async def rollback(self):
            calls.append("rollback")

    @asynccontextmanager
    async def factory():
        yield Session()

    async def guard(session):
        calls.append("guard")
        if refuse:
            raise db.PrivilegedRuntimeRole("unsafe_role")

    async def wrong_guard(session):
        raise AssertionError("Wrong authority selected")

    monkeypatch.setattr(db, "require_decision_runtime", guard if decision else wrong_guard)
    monkeypatch.setattr(db, "require_restricted_runtime", wrong_guard if decision else guard)
    database = object.__new__(db.Database)
    database.config = DatabaseConfig.from_serving_env(
        environ=serving_environment(decision=decision), decision=decision
    )
    database._disposed = False
    database._session_factory = factory
    if refuse:
        with pytest.raises(db.PrivilegedRuntimeRole):
            async with database.session():
                pytest.fail("Unsafe serving role reached a caller")
        assert calls == ["guard", "rollback"]
    else:
        async with database.session():
            calls.append("caller")
        assert calls == ["guard", "rollback", "caller", "rollback"]
