"""API foundation tests. Public smoke suite: no PostgreSQL, credentials, dataset or network."""

import logging
import os
import subprocess
import sys
from importlib.metadata import version

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rfq_copilot.api.app import TITLE, create_app, lifespan
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.routers.health import Liveness
from rfq_copilot.api.routers.system import SystemInfo, get_system_info

DEV = ApiSettings(environment="development")
PROD = ApiSettings(environment="production")
VERSION = version("engineering-rfq-copilot")


@pytest.fixture
def client():
    with TestClient(create_app(DEV)) as client:
        yield client


# --- Configuration -----------------------------------------------------------------------


def test_settings_default_to_production_without_docs():
    settings = ApiSettings.from_env({})
    assert settings.environment == "production"
    assert settings.docs_enabled is False


def test_settings_read_only_prefixed_variables():
    settings = ApiSettings.from_env(
        {"RFQ_API_ENVIRONMENT": "development", "DATABASE_URL": "x", "ENVIRONMENT": "y"}
    )
    assert settings == DEV
    assert settings.docs_enabled is True


def test_settings_read_process_environment(monkeypatch):
    monkeypatch.setenv("RFQ_API_ENVIRONMENT", "development")
    assert ApiSettings.from_env() == DEV
    monkeypatch.delenv("RFQ_API_ENVIRONMENT")
    assert ApiSettings.from_env() == PROD


@pytest.mark.parametrize(
    "environ",
    [
        {"RFQ_API_ENVIRONMENT": "staging"},
        {"RFQ_API_ENVIRONMENT": ""},
        {"RFQ_API_ENVIRONMENT": "Development"},
        {"RFQ_API_DEBUG": "true"},  # unknown setting: a typo must not be silently ignored
    ],
)
def test_invalid_settings_are_rejected(environ):
    with pytest.raises(ValidationError):
        ApiSettings.from_env(environ)


def test_invalid_process_settings_fail_application_creation(monkeypatch):
    monkeypatch.setenv("RFQ_API_ENVIRONMENT", "staging")
    with pytest.raises(ValidationError):
        create_app()


def test_settings_are_immutable():
    with pytest.raises(ValidationError):
        DEV.environment = "production"


# --- Application factory and lifespan ----------------------------------------------------


def test_create_app_metadata():
    app = create_app(DEV)
    assert (app.title, app.version, app.debug) == (TITLE, VERSION, False)


def test_factory_returns_independent_instances():
    first, second = create_app(DEV), create_app(DEV)
    assert first is not second
    first.dependency_overrides[get_system_info] = lambda: None
    assert second.dependency_overrides == {}


def test_importing_the_api_creates_no_application():
    import rfq_copilot.api.app as module

    assert not any(type(value).__name__ == "FastAPI" for value in vars(module).values())


def test_lifespan_runs_startup_and_shutdown_repeatedly(caplog):
    app = create_app(DEV)
    caplog.set_level(logging.INFO, logger="rfq_copilot.api.app")
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/health/live").status_code == 200
            assert caplog.messages[-1] == f"API startup: {TITLE} {VERSION}"
        assert caplog.messages[-1] == f"API shutdown: {TITLE} {VERSION}"
    assert len(caplog.messages) == 4


@pytest.mark.asyncio
async def test_lifespan_shutdown_runs_when_the_context_raises(caplog):
    app = create_app(DEV)
    caplog.set_level(logging.INFO, logger="rfq_copilot.api.app")
    with pytest.raises(RuntimeError, match="serving failed"):
        async with lifespan(app):
            raise RuntimeError("serving failed")
    assert caplog.messages == [
        f"API startup: {TITLE} {VERSION}",
        f"API shutdown: {TITLE} {VERSION}",
    ]


# --- Endpoints ---------------------------------------------------------------------------


def test_liveness(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "alive"}
    Liveness.model_validate(response.json())


def test_system_info_exposes_only_permitted_fields(client):
    response = client.get("/api/v1/system/info")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"name": TITLE, "version": VERSION, "api_version": "v1"}
    SystemInfo.model_validate(response.json())


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/health/live", "/api/v1/system/info"])
def test_read_only_routes_reject_mutating_methods(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"


@pytest.mark.parametrize("path", ["/", "/health", "/system/info", "/api/v2/system/info"])
def test_unknown_routes_are_404(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_dependency_override_replaces_system_info():
    app = create_app(DEV)
    stub = SystemInfo(name="stub", version="0.0.0", api_version="v1")
    app.dependency_overrides[get_system_info] = lambda: stub
    with TestClient(app) as client:
        assert client.get("/api/v1/system/info").json()["name"] == "stub"
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        assert client.get("/api/v1/system/info").json()["name"] == TITLE


def test_response_model_rejects_a_dependency_that_breaks_the_contract():
    app = create_app(DEV)
    app.dependency_overrides[get_system_info] = lambda: {"name": "x", "version": "1"}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/system/info")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"  # no traceback or internals leaked


# --- Routing and OpenAPI -----------------------------------------------------------------


# Day 18 added protected read routes; Day 19 added the attachment routes. The upload is
# the only non-GET operation.
ROUTES = {
    "/health/live": {"get"},
    "/api/v1/system/info": {"get"},
    "/api/v1/customers/me": {"get"},
    "/api/v1/rfqs": {"get"},
    "/api/v1/rfqs/{rfq_id}": {"get"},
    "/api/v1/materials": {"get"},
    "/api/v1/quotes/{quote_id}": {"get"},
    "/api/v1/rfqs/{rfq_id}/attachments": {"get", "post"},
}


def test_only_the_foundation_routes_are_registered():
    # The schema is generated even where it is not served: exactly these routes exist.
    paths = create_app(PROD).openapi()["paths"]
    assert {path: set(ops) for path, ops in paths.items()} == ROUTES


def test_openapi_schema_in_development(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"] == {
        "title": TITLE,
        "version": VERSION,
        "description": (
            "Deterministic RFQ backend. Customer-scoped routes require verified identity; "
            "the only write is an RFQ attachment upload."
        ),
    }
    assert set(schema["paths"]) == set(ROUTES)
    assert set(schema["paths"]["/health/live"]) == {"get"}
    assert schema["paths"]["/health/live"]["get"]["tags"] == ["health"]
    assert schema["paths"]["/api/v1/system/info"]["get"]["tags"] == ["system"]
    assert {tag["name"] for tag in schema["tags"]} == {
        "health",
        "system",
        "customers",
        "rfqs",
        "materials",
        "quotes",
        "attachments",
    }
    ok = schema["paths"]["/api/v1/system/info"]["get"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SystemInfo"
    }
    assert {"Liveness", "SystemInfo"} <= set(schema["components"]["schemas"])


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_interactive_docs_in_development(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_production_serves_no_docs_or_schema(path):
    with TestClient(create_app(PROD)) as client:
        assert client.get(path).status_code == 404
        assert client.get("/health/live").status_code == 200


def test_responses_disclose_no_environment_values(monkeypatch):
    secret = "postgresql+asyncpg://leak_role:leak_password@10.9.8.7/leak_db"
    monkeypatch.setenv("DATABASE_URL", secret)
    with TestClient(create_app(DEV)) as client:
        bodies = [client.get(p).text for p in ["/health/live", "/api/v1/system/info"]]
        bodies.append(client.get("/openapi.json").text)
    for body in bodies:
        assert not any(part in body for part in ["leak_", "10.9.8.7", "postgresql", os.getcwd()])


# --- Isolation from the database and the private dataset ---------------------------------

ISOLATION_PROBE = """
import sys
from fastapi.testclient import TestClient
from rfq_copilot.api.app import create_app
from rfq_copilot.api.config import ApiSettings

with TestClient(create_app(ApiSettings(environment="development"))) as client:
    codes = [client.get(p).status_code for p in
             ("/health/live", "/api/v1/system/info", "/openapi.json")]
assert codes == [200, 200, 200], codes
loaded = [m for m in ("sqlalchemy", "asyncpg", "dotenv", "rfq_copilot.database",
                      "rfq_copilot.config", "rfq_copilot.seeding") if m in sys.modules]
assert not loaded, loaded
"""


def test_serves_without_database_credentials_dataset_or_database_modules(tmp_path):
    # Fresh interpreter, empty working directory (no .env, no data/), no DB variables.
    env = {k: v for k, v in os.environ.items() if "DATABASE" not in k and k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", ISOLATION_PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
