"""Each PostgreSQL test owns its async engine on its function's event loop."""

from pathlib import Path

import pytest
import pytest_asyncio

from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def connection_config(target: str) -> DatabaseConfig:
    env_file = PROJECT_ROOT / ".env"
    return DatabaseConfig.from_env(target, env_file=env_file if env_file.is_file() else None)


@pytest.fixture
def development_config():
    return connection_config("development")


@pytest.fixture
def test_config():
    # Validation requires an explicit *_test name distinct from development.
    # Missing credentials are a failure, never a silent integration-test skip.
    return connection_config("test")


@pytest_asyncio.fixture
async def database(test_config):
    async with Database(test_config, read_only=True) as database:
        yield database
