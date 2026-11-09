"""Configuration and safe error boundaries; only synthetic credentials are used here."""

import traceback

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
from rfq_copilot.database import Database, DatabaseError


def example_environment():
    # These are intentionally fake unit-test fixtures, never development credentials.
    development = URL.create(
        "postgresql+asyncpg",
        username="example_role",
        password="example-password",
        host="127.0.0.1",
        port=5432,
        database="example_dev",
    )
    return {
        "DATABASE_URL": development.render_as_string(hide_password=False),
        "TEST_DATABASE_URL": development.set(database="example_test").render_as_string(
            hide_password=False
        ),
        "TEST_DATABASE_NAME": "example_test",
    }


def test_environment_file_is_explicit_and_process_variables_win(tmp_path):
    values = example_environment()
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(f"{key}='{value}'" for key, value in values.items()))
    override = values["DATABASE_URL"].replace("example_dev", "override_dev")
    config = DatabaseConfig.from_env(env_file=env_file, environ={"DATABASE_URL": override})
    assert config.url.database == "override_dev"
    assert (
        DatabaseConfig.from_env("test", env_file=env_file, environ={}).url.database
        == "example_test"
    )


def test_environment_file_is_not_discovered_implicitly(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DATABASE_URL='do-not-read-me'")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_URL is required"):
        DatabaseConfig.from_env(environ={})


def test_missing_explicit_environment_file_fails(tmp_path):
    with pytest.raises(DatabaseConfigurationError, match="does not exist"):
        DatabaseConfig.from_env(env_file=tmp_path / "missing", environ=example_environment())


def test_encoded_password_round_trip_and_redacted_settings():
    password = "synthetic@:/%#$ password"
    url = URL.create(
        "postgresql+asyncpg",
        username="example_role",
        password=password,
        host="127.0.0.1",
        database="example_dev",
    )
    config = DatabaseConfig.from_env(
        environ={"DATABASE_URL": url.render_as_string(hide_password=False)}
    )
    assert config.url.password == password
    assert "example_role" not in repr(config)
    assert password not in str(config)
    assert "postgresql" not in repr(config)


@pytest.mark.parametrize(
    ("changes", "target"),
    [
        ({"DATABASE_URL": ""}, "development"),
        ({"DATABASE_URL": "malformed-secret-example"}, "development"),
        ({"DATABASE_URL": "postgresql://example:example@localhost/example_dev"}, "development"),
        ({"DATABASE_URL": "postgresql+asyncpg://example@localhost/example_dev"}, "development"),
        (
            {"DATABASE_URL": "postgresql+asyncpg://example:example@localhost:70000/example_dev"},
            "development",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://example:example@localhost:notaport/example_dev"},
            "development",
        ),
        (
            {
                "DATABASE_URL": "postgresql+asyncpg://example:example@localhost/example_dev?password=x"
            },
            "development",
        ),
        ({}, "unrecognized"),
        ({"TEST_DATABASE_URL": ""}, "test"),
        ({"TEST_DATABASE_NAME": ""}, "test"),
        ({"TEST_DATABASE_NAME": "another_test"}, "test"),
        ({"DATABASE_URL": "postgresql+asyncpg://example:example@localhost/example_test"}, "test"),
        ({"TEST_DATABASE_NAME": "example_dev"}, "test"),
    ],
    ids=[
        "missing-url",
        "malformed-url",
        "sync-driver",
        "missing-password",
        "port-range",
        "port-syntax",
        "query-options",
        "unknown-target",
        "missing-test-url",
        "missing-test-name",
        "wrong-test-name",
        "same-database",
        "unsafe-test-name",
    ],
)
def test_invalid_or_unsafe_configuration_fails_without_echoing_values(changes, target):
    values = example_environment() | changes
    with pytest.raises(DatabaseConfigurationError) as error:
        DatabaseConfig.from_env(target, environ=values)
    assert "malformed-secret-example" not in str(error.value)
    assert "postgresql://" not in str(error.value)
    assert "postgresql+asyncpg://" not in str(error.value)


@pytest.mark.asyncio
async def test_database_errors_do_not_leak_original_diagnostics():
    secret = "synthetic-private-driver-message"
    async with Database(DatabaseConfig.from_env(environ=example_environment())) as database:
        with pytest.raises(DatabaseError) as error:
            async with database.session():
                raise SQLAlchemyError(secret)
        rendered = "".join(traceback.format_exception(error.value))
        assert secret not in rendered
        assert "SQLAlchemyError" not in rendered


@pytest.mark.asyncio
async def test_disposal_before_first_connection_is_idempotent():
    database = Database(DatabaseConfig.from_env(environ=example_environment()))
    await database.dispose()
    await database.dispose()
    with pytest.raises(DatabaseError, match="disposed"):
        async with database.session():
            pytest.fail("Disposed engines must not supply a new session")
