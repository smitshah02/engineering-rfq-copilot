"""G5: the workflow worker's restricted database configuration. No PostgreSQL required.

``DatabaseConfig.from_serving_env(workflow=True)`` reads exactly one credential
(``WORKFLOW_DATABASE_URL`` / ``TEST_WORKFLOW_DATABASE_URL``) plus non-secret target metadata,
never falls back to the owner, API or decision credential, and every ``Database`` session
on it passes the live ``require_workflow_runtime`` catalogue check before anything runs.
Role names prove nothing: an owner or API role behind the variable is refused live.
"""

import logging
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.engine import URL
from test_serving_database_config import SelectedKeysOnly

from rfq_copilot import database as db
from rfq_copilot import workflow_worker
from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError

SECRET = "synthetic-workflow-password"
OTHERS = {
    "DATABASE_URL": "postgresql+asyncpg://owner:owner-secret@127.0.0.1/rfq_copilot_dev",
    "APP_DATABASE_URL": "postgresql+asyncpg://app:app-secret@127.0.0.1/rfq_copilot_dev",
    "DECISION_DATABASE_URL": "postgresql+asyncpg://dec:dec-secret@127.0.0.1/rfq_copilot_dev",
    "TEST_DATABASE_URL": "postgresql+asyncpg://owner:owner-secret@127.0.0.1/rfq_copilot_test",
    "TEST_APP_DATABASE_URL": "postgresql+asyncpg://app:app-secret@127.0.0.1/rfq_copilot_test",
}


def workflow_environment(target="development", **over):
    variable, name = "WORKFLOW_DATABASE_URL", "rfq_copilot_dev"
    if target == "test":
        variable, name = "TEST_WORKFLOW_DATABASE_URL", "rfq_copilot_test"
    url = URL.create("postgresql+asyncpg", username="synthetic_workflow", password=SECRET,
                     host="127.0.0.1", port=5432, database=name)  # fmt: skip
    return {variable: url.render_as_string(hide_password=False), "TEST_DATABASE_NAME": "rfq_copilot_test", **over}  # fmt: skip


def secret_free(text: str) -> bool:
    return SECRET not in text and "secret" not in text and "://" not in text


@pytest.mark.parametrize("target", ["development", "test"])
def test_valid_worker_configuration_reads_only_its_own_credential(target):
    env = SelectedKeysOnly(workflow_environment(target) | OTHERS)
    config = DatabaseConfig.from_serving_env(target, environ=env, workflow=True)
    assert (config.workflow, config.runtime, config.decision) == (True, False, False)
    assert config.url.username == "synthetic_workflow" and config.target == target
    selected = "TEST_WORKFLOW_DATABASE_URL" if target == "test" else "WORKFLOW_DATABASE_URL"
    assert env.read <= {selected, "DATABASE_HOST", "DATABASE_PORT", "DEVELOPMENT_DATABASE_NAME", "TEST_DATABASE_NAME"}  # fmt: skip
    assert not env.read & set(OTHERS)  # owner, API and decision credentials never touched
    assert secret_free(repr(config)) and secret_free(str(config))


@pytest.mark.parametrize("target", ["development", "test"])
def test_missing_worker_url_never_falls_back_to_a_privileged_credential(target):
    env = SelectedKeysOnly({"TEST_DATABASE_NAME": "rfq_copilot_test", **OTHERS})
    with pytest.raises(DatabaseConfigurationError, match="WORKFLOW_DATABASE_URL is required") as caught:  # fmt: skip
        DatabaseConfig.from_serving_env(target, environ=env, workflow=True)
    assert not env.read & set(OTHERS) and secret_free(str(caught.value))


def test_the_other_target_variable_is_never_used():
    development_only = workflow_environment("development")  # WORKFLOW_DATABASE_URL only
    with pytest.raises(DatabaseConfigurationError, match="TEST_WORKFLOW_DATABASE_URL is required"):
        DatabaseConfig.from_serving_env("test", environ=development_only, workflow=True)


@pytest.mark.parametrize(
    "change",
    [
        {"WORKFLOW_DATABASE_URL": "not-a-url"},
        {"WORKFLOW_DATABASE_URL": "postgresql://w:" + SECRET + "@127.0.0.1/rfq_copilot_dev"},
        {"WORKFLOW_DATABASE_URL": "postgresql+asyncpg://w@127.0.0.1/rfq_copilot_dev"},
        {
            "WORKFLOW_DATABASE_URL": "postgresql+asyncpg://w:"
            + SECRET
            + "@127.0.0.1/rfq_copilot_test"
        },  # fmt: skip
        {"DATABASE_HOST": "other.invalid"},
        {"DATABASE_PORT": "5433"},
        {"DATABASE_PORT": "not-a-port"},
        {"DEVELOPMENT_DATABASE_NAME": "another_database"},
    ],
)
def test_wrong_environment_or_target_fails_without_echoing_values(change):
    env = workflow_environment() | change
    with pytest.raises(DatabaseConfigurationError) as caught:
        DatabaseConfig.from_serving_env(environ=env, workflow=True)
    assert secret_free(str(caught.value))


@pytest.mark.parametrize("name", [None, "rfq_copilot_dev", "wrong_test"])
def test_test_target_keeps_the_explicit_test_database_guards(name):
    env = workflow_environment("test")
    if name is None:
        del env["TEST_DATABASE_NAME"]
    else:
        env["TEST_DATABASE_NAME"] = name
    with pytest.raises(DatabaseConfigurationError):
        DatabaseConfig.from_serving_env("test", environ=env, workflow=True)


def test_one_restricted_credential_only():
    with pytest.raises(DatabaseConfigurationError, match="one restricted credential"):
        DatabaseConfig.from_serving_env(environ=workflow_environment(), workflow=True, decision=True)  # fmt: skip
    config = DatabaseConfig.from_serving_env(environ=workflow_environment(), workflow=True)
    for flags in ({"runtime": True}, {"decision": True}):
        with pytest.raises(DatabaseConfigurationError, match="one restricted credential"):
            DatabaseConfig(url=config.url, workflow=True, **flags)


def workflow_database(factory):
    database = object.__new__(db.Database)
    database.config = DatabaseConfig.from_serving_env(environ=workflow_environment(), workflow=True)
    database._disposed = False
    database._session_factory = factory
    return database


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse", [False, True])
async def test_every_worker_session_passes_the_workflow_check_before_yield(monkeypatch, refuse):
    calls = []

    class Session:
        async def rollback(self):
            calls.append("rollback")

    @asynccontextmanager
    async def factory():
        yield Session()

    async def guard(session):
        calls.append("workflow_guard")
        if refuse:
            raise db.PrivilegedRuntimeRole("unsafe_role")

    async def wrong(session):
        raise AssertionError("the worker must be checked as the workflow role")

    monkeypatch.setattr(db, "require_workflow_runtime", guard)
    monkeypatch.setattr(db, "require_restricted_runtime", wrong)
    monkeypatch.setattr(db, "require_decision_runtime", wrong)
    database = workflow_database(factory)
    if refuse:
        with pytest.raises(db.PrivilegedRuntimeRole):
            async with database.session():
                pytest.fail("an unsafe role reached the worker")
        assert calls == ["workflow_guard", "rollback"]
    else:
        async with database.session():
            calls.append("worker")
        assert calls == ["workflow_guard", "rollback", "worker", "rollback"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "violations",
    [
        ("database_owner", "table_owner"),  # the owner credential behind WORKFLOW_DATABASE_URL
        ("insert_privileges",),  # the API role: RFQ, quote and run INSERTs
        ("update_privileges",),  # e.g. quote status or RFQ columns
        ("decision_membership",),  # a member of rfq_quote_decision
    ],
)
async def test_owner_api_and_decision_roles_are_refused_live(monkeypatch, violations, caplog):
    seen = {}

    async def catalogue(session, *, decision=False, workflow=False):
        seen.update(decision=decision, workflow=workflow)
        return violations

    monkeypatch.setattr(db, "runtime_role_violations", catalogue)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(db.PrivilegedRuntimeRole, match="restricted workflow role") as caught:
        await db.require_workflow_runtime(object())
    assert seen == {"decision": False, "workflow": True}  # the worker's allowlists, never the API's
    assert all(code in str(caught.value) for code in violations)
    assert secret_free(str(caught.value)) and secret_free(caplog.text)


@pytest.mark.asyncio
async def test_a_restricted_role_passes(monkeypatch):
    async def catalogue(session, *, decision=False, workflow=False):
        return ()

    monkeypatch.setattr(db, "runtime_role_violations", catalogue)
    await db.require_workflow_runtime(object())  # no exception


def test_the_worker_entry_point_selects_the_workflow_credential(monkeypatch, caplog):
    seen = {}

    class Stop(Exception):
        pass

    def from_serving_env(target="development", *, environ=None, decision=False, workflow=False):
        seen.update(target=target, decision=decision, workflow=workflow)
        raise Stop

    monkeypatch.setattr(workflow_worker, "_intake_graph_from_env", lambda environ: object())
    monkeypatch.setattr(workflow_worker, "_mcp_from_env", lambda environ: lambda principal: None)
    monkeypatch.setattr(DatabaseConfig, "from_serving_env", staticmethod(from_serving_env))
    for key, value in OTHERS.items():
        monkeypatch.setenv(key, value)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(Stop):
        workflow_worker.main(["--once"])
    assert seen == {"target": "development", "decision": False, "workflow": True}
    assert secret_free(caplog.text)
