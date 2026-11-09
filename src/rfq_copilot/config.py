"""Explicit, credential-safe configuration for the asynchronous database layer."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

DatabaseTarget = Literal["development", "test"]


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

    def __post_init__(self) -> None:
        _validate_url(self.url)
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
    def from_env(
        cls,
        target: DatabaseTarget = "development",
        *,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "DatabaseConfig":
        """Read only an explicitly supplied dotenv file; process variables take precedence."""
        if target not in ("development", "test"):
            raise DatabaseConfigurationError("Database target must be development or test.")
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
            return cls(url=development, target=target)
        testing = _parse_url(values.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL")
        return cls(
            url=testing,
            target=target,
            expected_test_database=values.get("TEST_DATABASE_NAME"),
            development_database=development.database,
        )
