"""Day 22 backend integration: public checks (no PostgreSQL, no dataset, no credentials).

Fills the two gaps left by Days 16-21 and Phase 3.5: the OpenAPI matrix test cannot see a
route registered with include_in_schema=False, and nothing started a real Uvicorn process.
"""

import os
import signal
import socket
import subprocess
import sys
import time

import httpx2
from fastapi.routing import APIRoute
from test_auth import trust as trust
from test_rbac import MATRIX, PUBLIC, concrete

from rfq_copilot.api.app import create_app

DOCS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _walk(routes, prefix, operations, other):
    # FastAPI nests included routers (_IncludedRouter); recurse so nothing is skipped.
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            _walk(included.routes, prefix + route.include_context.prefix, operations, other)
        elif isinstance(route, APIRoute):
            operations |= {(m, prefix + route.path) for m in route.methods}
        else:
            other.append(prefix + route.path)


def test_every_registered_route_is_public_by_name_or_rejects_anonymous_callers(trust):
    # Walks app.routes, not the schema: a hidden route would bypass the OpenAPI matrix.
    app = create_app(trust.settings())
    operations, other = set(), []
    _walk(app.routes, "", operations, other)
    assert operations - PUBLIC == set(MATRIX)
    assert set(other) <= DOCS
    transport = httpx2.ASGITransport(app=app)

    async def anonymous():
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as client:
            return [(await client.request(m, concrete(p))).status_code for m, p in sorted(MATRIX)]

    import asyncio

    assert set(asyncio.run(anonymous())) == {401}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_uvicorn_serves_and_shuts_down_cleanly_without_database_credentials(tmp_path):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RFQ_", "DATABASE"))}
    env["RFQ_API_ENVIRONMENT"] = "production"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "--factory", "rfq_copilot.api.app:create_app",
         "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                live = httpx2.get(f"http://127.0.0.1:{port}/health/live", timeout=1)
                break
            except httpx2.TransportError:
                assert proc.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
        assert live.status_code == 200 and live.json() == {"status": "alive"}
        base = f"http://127.0.0.1:{port}"
        assert httpx2.get(f"{base}/openapi.json").status_code == 404  # production: no docs
        assert httpx2.get(f"{base}/api/v1/customers/me").status_code == 401
        no_trust = httpx2.get(f"{base}/api/v1/customers/me", headers={"Authorization": "Bearer x"})
        assert no_trust.status_code == 503  # trust boundary unconfigured fails closed
    finally:
        proc.send_signal(signal.SIGTERM)
        output = proc.communicate(timeout=20)[0].decode()
    # Uvicorn re-raises the handled SIGTERM after a graceful shutdown, so -SIGTERM is clean.
    assert proc.returncode in (0, -signal.SIGTERM), output
    assert "Application shutdown complete" in output
    assert "Traceback" not in output
