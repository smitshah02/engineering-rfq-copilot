"""ASGI application factory.

Serve with ``uvicorn --factory rfq_copilot.api.app:create_app``. There is no module-level
application: settings are read when an application is created, never at import.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.errors import request_validation_handler
from rfq_copilot.api.routers import (
    attachments,
    customers,
    health,
    materials,
    quotes,
    rfqs,
    system,
)
from rfq_copilot.api.tokens import TokenVerifier

if TYPE_CHECKING:
    from rfq_copilot.api.mcp_integration import McpIntegration
    from rfq_copilot.database import Database

TITLE = "Engineering RFQ Copilot API"
logger = logging.getLogger(__name__)

TAGS = [
    {"name": "health", "description": "Process liveness. No downstream checks."},
    {"name": "system", "description": "Non-sensitive service metadata."},
    {"name": "customers", "description": "The verified customer's profile. Requires identity."},
    {"name": "rfqs", "description": "The verified customer's RFQs. Requires identity."},
    {"name": "materials", "description": "Material catalog identities. Requires identity."},
    {
        "name": "quotes",
        "description": "Released quotes of the verified customer. Requires identity.",
    },
    {
        "name": "attachments",
        "description": "PDF and CSV BOM attachments of the verified customer's RFQs. Requires identity.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup opens nothing: no engine, pool, migration, seeding or rule registration.
    # A database is created lazily by the first authorized protected request (api/db.py).
    logger.info("API startup: %s %s", app.title, app.version)
    try:
        yield
    finally:
        # Future resources are released here, so cleanup runs even when serving fails.
        # Only a Database the application created itself is disposed; an injected one
        # belongs to its caller.
        try:
            if app.state.owns_database and app.state.database is not None:
                database, app.state.database = app.state.database, None
                app.state.owns_database = False
                await database.dispose()
        finally:
            if app.state.owns_write_database and app.state.write_database is not None:
                database, app.state.write_database = app.state.write_database, None
                app.state.owns_write_database = False
                await database.dispose()
        logger.info("API shutdown: %s %s", app.title, app.version)


def create_app(
    settings: ApiSettings | None = None,
    *,
    database: "Database | None" = None,
    mcp: "McpIntegration | None" = None,
) -> FastAPI:
    """Build an independent application; ``None`` reads ``RFQ_API_*`` process variables.

    ``database`` is an optional caller-owned ``Database``. Without one, protected routes
    build a read-only one lazily on first authorized use (see ``api/db.py``). Attachment
    uploads use a separate writable ``Database``, also built lazily and app-owned.
    """
    settings = ApiSettings.from_env() if settings is None else settings
    docs = settings.docs_enabled
    app = FastAPI(
        title=TITLE,
        version=version("engineering-rfq-copilot"),
        description=(
            "Deterministic RFQ backend. Customer-scoped routes require verified identity; "
            "the only write is an RFQ attachment upload."
        ),
        openapi_tags=TAGS,
        openapi_url="/openapi.json" if docs else None,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        lifespan=lifespan,
        exception_handlers={RequestValidationError: request_validation_handler},
    )
    app.state.settings = settings
    # Loads and checks the public key now: an unsafe key stops startup. None when the
    # trust boundary is not configured (bearer requests then answer 503).
    app.state.token_verifier = TokenVerifier.from_settings(settings)
    app.state.database = database
    app.state.owns_database = False
    app.state.write_database = None
    app.state.owns_write_database = False
    app.state.database_lock = asyncio.Lock()
    app.include_router(health.router)
    prefix = f"/api/{system.API_VERSION}"
    for module in (system, customers, rfqs, materials, quotes, attachments):
        app.include_router(module.router, prefix=prefix)
    # Day 29: the MCP-backed route exists only with an explicit integration (a trusted
    # credential provider). Without one it is not mounted at all: fail closed.
    app.state.mcp = mcp
    if mcp is not None:
        app.include_router(materials.mcp_router, prefix=prefix)
    return app
