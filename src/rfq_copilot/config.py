"""Explicit, credential-safe configuration for the asynchronous database layer.

Administrative and restricted serving credentials:

- ``DATABASE_URL`` / ``TEST_DATABASE_URL``: the database and table owner. Migrations,
  seeding, integrity audits, operator CLIs and test fixtures use it (``migration_safety``
  requires owner = current_user). It can switch triggers off, so binding work never uses it.
- ``APP_DATABASE_URL`` / ``TEST_APP_DATABASE_URL`` (``from_env(..., runtime=True)``): the
  restricted runtime role (``docs/database/k2_runtime_roles.sql``). It must be another role
  on the same database; nothing falls back to the owner when it is missing.
  ``database.require_restricted_runtime`` re-checks the live role's privileges.
- ``DECISION_DATABASE_URL`` / ``TEST_DECISION_DATABASE_URL`` (``from_env(..., decision=True)``,
  K5): the decision role, a member of ``rfq_quote_decision``
  (``docs/database/k5_decision_roles.sql``). Read only by the human decision process
  (``api/decision_app.py``); it must differ from both the owner and the runtime role.
  ``database.require_decision_runtime`` re-checks it live.

``from_env`` remains the administrative/test provisioning path, including its owner-to-role
comparisons. Serving factories use ``from_serving_env`` instead: it reads only the selected
restricted URL and non-secret target metadata, never an owner or another role's credential.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

DatabaseTarget = Literal["development", "test"]
RUNTIME_VARIABLES = {"development": "APP_DATABASE_URL", "test": "TEST_APP_DATABASE_URL"}
DECISION_VARIABLES = {
    "development": "DECISION_DATABASE_URL",
    "test": "TEST_DECISION_DATABASE_URL",
}
# Checkpoint A1/A3: the workflow worker role (docs/database/a1_workflow_role.sql).
WORKFLOW_VARIABLES = {
    "development": "WORKFLOW_DATABASE_URL",
    "test": "TEST_WORKFLOW_DATABASE_URL",
}
# Checkpoint A4: the quotation-drafter role (docs/database/a4_drafter_role.sql).
DRAFTER_VARIABLES = {
    "development": "DRAFTER_DATABASE_URL",
    "test": "TEST_DRAFTER_DATABASE_URL",
}


class DatabaseConfigurationError(ValueError):
    """Invalid or unsafe database configuration; messages never contain a URL."""


def _parse_url(value: str | None, variable: str) -> URL:
    if not value:
        raise DatabaseConfigurationError(f"{variable} is required.")
    try:
        url = make_url(value)
    except (ArgumentError, ValueError, TypeError):
        raise DatabaseConfigurationError(f"{variable} is not a valid database URL.") from None
    _validate_url(url)
    return url


def _validate_url(url: URL) -> None:
    if not isinstance(url, URL) or url.drivername != "postgresql+asyncpg":
        raise DatabaseConfigurationError("Use the postgresql+asyncpg driver.")
    if not all([url.username, url.password, url.host, url.database]):
        raise DatabaseConfigurationError(
            "Database URL requires a role, password, host and database."
        )
    if any(
        char in str(value)
        for value in [url.username, url.host, url.database]
        for char in ["\n", "\r", "\x00"]
    ):
        raise DatabaseConfigurationError(
            "Database identifiers must not contain control characters."
        )
    if url.port is not None and not 1 <= url.port <= 65535:
        raise DatabaseConfigurationError("Database port must be between 1 and 65535.")
    if url.query:
        raise DatabaseConfigurationError(
            "URL query options are unsupported; use typed engine options."
        )


@dataclass(frozen=True)
class DatabaseConfig:
    """Validated settings. Both string representations deliberately omit the URL."""

    url: URL = field(repr=False)
    target: DatabaseTarget = "development"
    expected_test_database: str | None = field(default=None, repr=False)
    development_database: str | None = field(default=None, repr=False)
    runtime: bool = False  # True: the restricted runtime role, never the owner
    decision: bool = False  # True (K5): the decision role, never the owner or runtime role
    workflow: bool = False  # True (A1): the workflow worker role, checked live per session
    drafter: bool = False  # True (A4): the quotation-drafter role, checked live per session

    def __post_init__(self) -> None:
        _validate_url(self.url)
        if self.runtime + self.decision + self.workflow + self.drafter > 1:
            raise DatabaseConfigurationError("Choose one restricted credential.")
        if self.target not in ("development", "test"):
            raise DatabaseConfigurationError("Database target must be development or test.")
        if self.target == "test":
            if not self.expected_test_database or not self.expected_test_database.endswith("_test"):
                raise DatabaseConfigurationError("TEST_DATABASE_NAME must explicitly end in _test.")
            if self.url.database != self.expected_test_database:
                raise DatabaseConfigurationError("Test URL does not match TEST_DATABASE_NAME.")
            if not self.development_database or self.url.database == self.development_database:
                raise DatabaseConfigurationError("Test and development databases must be distinct.")

    @classmethod
    def from_serving_env(
        cls,
        target: DatabaseTarget = "development",
        *,
        environ: Mapping[str, str] | None = None,
        decision: bool = False,
        workflow: bool = False,
        drafter: bool = False,
    ) -> "DatabaseConfig":
        """Select one restricted credential without dotenv discovery or owner access.

        Non-secret DATABASE_HOST/PORT default to loopback:5432; DEVELOPMENT_DATABASE_NAME
        defaults to rfq_copilot_dev. Test serving additionally requires TEST_DATABASE_NAME.
        URL role names do not prove privilege isolation: Database.session checks the live
        catalogs before yielding a restricted session, including decision-group membership.
        """
        if target not in ("development", "test"):
            raise DatabaseConfigurationError("Database target must be development or test.")
        if decision + workflow + drafter > 1:
            raise DatabaseConfigurationError("Choose one restricted credential.")
        source = os.environ if environ is None else environ
        variables = (
            DECISION_VARIABLES if decision else WORKFLOW_VARIABLES if workflow
            else DRAFTER_VARIABLES if drafter else RUNTIME_VARIABLES
        )  # fmt: skip
        variable = variables[target]
        url = _parse_url(source.get(variable), variable)
        development = source.get("DEVELOPMENT_DATABASE_NAME", "rfq_copilot_dev")
        expected = development if target == "development" else source.get("TEST_DATABASE_NAME")
        host = source.get("DATABASE_HOST", "127.0.0.1")
        try:
            port = int(source.get("DATABASE_PORT", "5432"))
        except (TypeError, ValueError):
            raise DatabaseConfigurationError("DATABASE_PORT must be a valid port.") from None
        if not 1 <= port <= 65535 or not development or not host:
            raise DatabaseConfigurationError("Restricted database target metadata is invalid.")
        if (url.host, url.port or 5432, url.database) != (host, port, expected):
            raise DatabaseConfigurationError(
                f"{variable} does not match the configured restricted database target."
            )
        return cls(
            url=url,
            target=target,
            expected_test_database=expected if target == "test" else None,
            development_database=development,
            runtime=not (decision or workflow or drafter),
            decision=decision,
            workflow=workflow,
            drafter=drafter,
        )

    @classmethod
    def from_env(
        cls,
        target: DatabaseTarget = "development",
        *,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
        runtime: bool = False,
        decision: bool = False,
    ) -> "DatabaseConfig":
        """Read only an explicitly supplied dotenv file; process variables take precedence.

        ``runtime=True`` returns the restricted runtime credential for the same target. The
        owner URL is still required and validated: the runtime URL must name the same host,
        port and database with a different role. ``decision=True`` (K5) returns the decision
        credential, which must also differ from the runtime role when that one is set.
        """
        if target not in ("development", "test"):
            raise DatabaseConfigurationError("Database target must be development or test.")
        if runtime and decision:
            raise DatabaseConfigurationError("Choose the runtime or the decision credential.")
        values: dict[str, str | None] = {}
        if env_file is not None:
            if not env_file.is_file():
                raise DatabaseConfigurationError("The requested environment file does not exist.")
            try:
                values.update(dotenv_values(env_file, interpolate=False))
            except (OSError, UnicodeError):
                raise DatabaseConfigurationError(
                    "The environment file could not be read."
                ) from None
        values.update(os.environ if environ is None else environ)
        development = _parse_url(values.get("DATABASE_URL"), "DATABASE_URL")
        if target == "development":
            owner = cls(url=development, target=target)
        else:
            owner = cls(
                url=_parse_url(values.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL"),
                target=target,
                expected_test_database=values.get("TEST_DATABASE_NAME"),
                development_database=development.database,
            )
        if not (runtime or decision):
            return owner
        variable = (DECISION_VARIABLES if decision else RUNTIME_VARIABLES)[target]
        url = _parse_url(values.get(variable), variable)
        if url.username == owner.url.username:
            raise DatabaseConfigurationError(f"{variable} must not use the owner role.")
        if (url.host, url.port, url.database) != (
            owner.url.host,
            owner.url.port,
            owner.url.database,
        ):
            raise DatabaseConfigurationError(f"{variable} must name the owner URL's database.")
        if decision:
            app = values.get(RUNTIME_VARIABLES[target])
            if app and _parse_url(app, RUNTIME_VARIABLES[target]).username == url.username:
                raise DatabaseConfigurationError(f"{variable} must not use the runtime role.")
            return replace(owner, url=url, decision=True)
        return replace(owner, url=url, runtime=True)
