"""MCP host (Day 28): one ASGI process, three SDK Streamable HTTP apps, a guarded edge.

Endpoints (exact paths; no trailing-slash variants, no other routes):
    POST /mcp/manufacturing   /mcp/supply   /mcp/pricing
    GET  /.well-known/oauth-protected-resource/mcp/{manufacturing,supply,pricing}

Routing is in-process: the gateway hands the ASGI scope to the matching SDK app. It never
proxies over the network, never forwards credentials elsewhere, and never authorizes:
each SDK app runs its own bearer authentication (``McpTokenVerifier``) and every tool
its own v2 permission check (``runtime._authorized``).

Edge controls, in order (each rejection is a fixed JSON body, nothing echoed):
    Host allowlist (421) -> Origin allowlist (403) -> exact route (404) ->
    per-client rate limit (429) -> concurrency limit (503) ->
    MCP endpoints only: POST (405), MCP-Protocol-Version == 2026-07-28 (400),
    body within the limit (413), a single JSON-RPC object that is not ``initialize``,
    whose ``_meta`` version and Mcp-Method / Mcp-Name headers match the body (400)
    -> timeout (504 if no response started).
The SDK then repeats Host/Origin (``TransportSecuritySettings``) and the body limit
(``max_request_body_size``), and owns all JSON-RPC parsing and transport.

Run (loopback development):
    uvicorn --factory rfq_copilot.mcp.host:create_app_from_env \
        --host 127.0.0.1 --port 8765 --no-proxy-headers --workers 1
``--no-proxy-headers`` matters: uvicorn otherwise rewrites the client address from
X-Forwarded-For. Rate and concurrency limits are per process (in memory).
"""

import ipaddress
import json
import logging
import os
import time
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Annotated, Any
from urllib.parse import urlsplit

import anyio
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database
from rfq_copilot.mcp.manufacturing import create_manufacturing_server
from rfq_copilot.mcp.pricing import create_pricing_server
from rfq_copilot.mcp.security import McpAuthSettings, McpTokenVerifier
from rfq_copilot.mcp.supply import create_supply_server

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2026-07-28"  # approved policy: modern revision only
METADATA_PREFIX = "/.well-known/oauth-protected-resource"
SERVERS = {
    "/mcp/manufacturing": create_manufacturing_server,
    "/mcp/supply": create_supply_server,
    "/mcp/pricing": create_pricing_server,
}
HOST_ENV_PREFIX = "RFQ_MCP_HOST_"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class McpHostSettings(BaseModel):
    """Edge settings. Non-loopback deployment needs https behind a declared TLS proxy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    public_base_url: AnyHttpUrl  # scheme://host[:port] clients use; no path
    allowed_hosts: Annotated[tuple[str, ...], Field(min_length=1, max_length=16)]
    allowed_origins: Annotated[tuple[str, ...], Field(max_length=16)] = ()
    trusted_proxies: Annotated[
        tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...], Field(max_length=16)
    ] = ()
    tls_terminating_proxy: bool = False
    max_body_bytes: Annotated[int, Field(ge=1024, le=1024 * 1024)] = 256 * 1024
    max_concurrent_requests: Annotated[int, Field(ge=1, le=256)] = 32
    rate_limit_per_minute: Annotated[int, Field(ge=1, le=10_000)] = 120
    request_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0

    @field_validator("allowed_hosts", "allowed_origins")
    @classmethod
    def _no_wildcards(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not v or "*" in v or v != v.strip() for v in values):
            raise ValueError("Allowlists take exact, nonblank values; no wildcards.")
        return values

    @model_validator(mode="after")
    def _secure_boundary(self) -> "McpHostSettings":
        url = urlsplit(str(self.public_base_url))
        if url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("public_base_url must be scheme://host[:port] only.")
        if url.netloc not in self.allowed_hosts:
            raise ValueError("allowed_hosts must contain the public_base_url host[:port].")
        if not _loopback(url.hostname or ""):
            if url.scheme != "https" or not self.tls_terminating_proxy or not self.trusted_proxies:
                raise ValueError(
                    "Non-loopback deployment requires https, tls_terminating_proxy=true "
                    "and the proxy's address in trusted_proxies."
                )
        return self

    @property
    def base(self) -> str:
        return str(self.public_base_url).rstrip("/")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "McpHostSettings":
        values: dict[str, Any] = {
            k.removeprefix(HOST_ENV_PREFIX).lower(): v
            for k, v in environ.items()
            if k.startswith(HOST_ENV_PREFIX)
        }
        for name in ("allowed_hosts", "allowed_origins", "trusted_proxies"):
            if name in values:
                values[name] = tuple(p.strip() for p in values[name].split(",") if p.strip())
        return cls(**values)


class _RateLimiter:
    """Fixed one-minute window per client key. ponytail: in-memory and per process; use a
    shared store (or the proxy's limiter) before running several workers."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute, self.windows = per_minute, {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self.windows) > 10_000:  # bound memory: drop expired windows
            self.windows = {k: w for k, w in self.windows.items() if now - w[0] < 60}
        start, count = self.windows.get(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        if count >= self.per_minute:
            return False
        self.windows[key] = (start, count + 1)
        return True


_REJECT = {
    400: "Request rejected by the MCP protocol policy.",
    403: "Origin not allowed.",
    404: "Not found.",
    405: "Method not allowed.",
    413: "Request body too large.",
    421: "Host not allowed.",
    429: "Too many requests.",
    503: "Server busy.",
    504: "Request timed out.",
}


async def _reject(send, status: int) -> None:
    body = json.dumps({"error": _REJECT[status]}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if status == 405:
        headers.append((b"allow", b"POST"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _policy_ok(headers: dict[str, str], body: bytes) -> bool:
    """Modern-only protocol policy over the raw request; the SDK still parses it fully."""
    if headers.get("mcp-protocol-version") != PROTOCOL_VERSION:
        return False  # legacy (absent or handshake-era) and unknown versions
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return False  # includes batches, which this revision does not use
    method, params = message.get("method"), message.get("params")
    if not isinstance(method, str) or method == "initialize":
        return False  # responses from clients and the legacy handshake are not accepted
    params = params if isinstance(params, dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    if "id" in message and meta.get("io.modelcontextprotocol/protocolVersion") != PROTOCOL_VERSION:
        return False
    if headers.get("mcp-method") != method:
        return False  # routing headers must describe the body, never replace it
    if method == "tools/call" and headers.get("mcp-name") != params.get("name"):
        return False
    return True


class McpGateway:
    """The ASGI host. Build it with ``create_mcp_host``; it refuses unsafe settings."""

    def __init__(
        self,
        host: McpHostSettings,
        auth: McpAuthSettings,
        database: Database,
        *,
        owns_database: bool = False,
    ) -> None:
        # Production passes a read-only ``Database``: ``snapshot_session`` for business
        # evaluations, ``session`` for identity reads. Both factories are required.
        if not all(callable(getattr(database, n, None)) for n in ("session", "snapshot_session")):
            raise TypeError("The MCP host needs snapshot and identity session factories.")
        if (auth.permission_policy, auth.token_revocation) != ("v2", "enforced"):
            raise ValueError("The MCP host requires RBAC v2 and enforced revocation.")
        if urlsplit(str(auth.resource_server_url)).netloc != urlsplit(host.base).netloc:
            raise ValueError("The MCP resource URL must be on public_base_url.")
        self.settings, self.database, self._owns_database = host, database, owns_database
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(host.allowed_hosts),
            allowed_origins=list(host.allowed_origins),
        )
        self.servers, self.routes = {}, {}
        for path, factory in SERVERS.items():
            resource = auth.model_copy(update={"resource_server_url": f"{host.base}{path}"})
            verifier = McpTokenVerifier.from_settings(resource, database.session)
            server = factory(database.snapshot_session, verifier)
            app = server.streamable_http_app(
                streamable_http_path=path,
                max_request_body_size=host.max_body_bytes,
                transport_security=security,
            )
            self.servers[path] = server
            self.routes[path] = self.routes[f"{METADATA_PREFIX}{path}"] = app
        self._limiter = _RateLimiter(host.rate_limit_per_minute)
        self._active = 0
        self._trusted = {str(ip) for ip in host.trusted_proxies}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            return await self._lifespan(receive, send)
        if scope["type"] != "http":
            return  # no websockets or other protocols
        await self._http(scope, receive, send)

    async def _lifespan(self, receive, send) -> None:
        stack = AsyncExitStack()
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    if self._owns_database:
                        await stack.enter_async_context(self.database)
                    for server in self.servers.values():  # the SDK's mounted-app pattern
                        await stack.enter_async_context(server.session_manager.run())
                except Exception:
                    logger.exception("MCP host startup failed")
                    await stack.aclose()
                    await send({"type": "lifespan.startup.failed", "message": "startup failed"})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await stack.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    def _client_key(self, scope, headers: dict[str, str]) -> str:
        peer = (scope.get("client") or ("unknown", 0))[0]
        if peer in self._trusted and "x-forwarded-for" in headers:
            # Rightmost hop not added by a trusted proxy; anything else is caller-controlled.
            for hop in reversed([h.strip() for h in headers["x-forwarded-for"].split(",")]):
                if hop and hop not in self._trusted:
                    return hop
        return peer

    async def _http(self, scope, receive, send) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if headers.get("host") not in self.settings.allowed_hosts:
            return await _reject(send, 421)
        if "origin" in headers and headers["origin"] not in self.settings.allowed_origins:
            return await _reject(send, 403)
        app = self.routes.get(scope["path"])
        if app is None:
            return await _reject(send, 404)
        if not self._limiter.allow(self._client_key(scope, headers)):
            return await _reject(send, 429)
        if self._active >= self.settings.max_concurrent_requests:
            return await _reject(send, 503)
        self._active += 1
        try:
            if scope["path"].startswith(METADATA_PREFIX):
                if scope["method"] not in ("GET", "HEAD"):
                    return await _reject(send, 405)
                return await self._forward(app, scope, receive, send)
            if scope["method"] != "POST":
                return await _reject(send, 405)  # no legacy GET stream or DELETE
            if headers.get("mcp-protocol-version") != PROTOCOL_VERSION:
                return await _reject(send, 400)
            body = await self._read_body(receive, headers)
            if body is None:
                return await _reject(send, 413)
            if not _policy_ok(headers, body):
                return await _reject(send, 400)
            await self._forward(app, scope, _replay(body, receive), send)
        finally:
            self._active -= 1

    async def _read_body(self, receive, headers: dict[str, str]) -> bytes | None:
        limit = self.settings.max_body_bytes
        declared = headers.get("content-length", "")
        if declared and (not declared.isdigit() or int(declared) > limit):
            return None
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return b""
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
            if not message.get("more_body"):
                return b"".join(chunks)

    async def _forward(self, app, scope, receive, send) -> None:
        started = False

        async def tracked(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        with anyio.move_on_after(self.settings.request_timeout_seconds) as scope_timer:
            await app(scope, receive, tracked)
        if scope_timer.cancelled_caught and not started:
            await _reject(send, 504)


def _replay(body: bytes, original):
    """The already-read body once, then the real channel (so real disconnects arrive)."""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original()

    return receive


def create_mcp_host(
    host: McpHostSettings, auth: McpAuthSettings, database: Database, *, owns_database: bool = False
) -> McpGateway:
    return McpGateway(host, auth, database, owns_database=owns_database)


def create_app_from_env() -> McpGateway:
    """Deployment entry point; any missing or unsafe setting raises before serving."""
    environ = os.environ
    auth = McpAuthSettings.from_env(environ)
    host = McpHostSettings.from_env(environ)
    target = environ.get("RFQ_MCP_DATABASE_TARGET", "development")
    database = Database(DatabaseConfig.from_env(target), read_only=True)
    return create_mcp_host(host, auth, database, owns_database=True)
