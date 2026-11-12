"""REST -> MCP delegation (Day 29).

The REST bearer token is never forwarded: its audience is the REST API, and the MCP host
refuses it. A ``McpCredentialProvider`` must obtain a short-lived token for the MCP
audience, for exactly the verified REST principal (same ``sub``). No provider ships in this
repository: the identity provider offers no token exchange yet, and this service only
verifies tokens and never mints them. Without a provider there is no integration and the
MCP-backed route is not mounted (``create_app(mcp=None)``).

The guard here is a consistency check on what the provider returned, not authentication:
the MCP host still verifies the signature, issuer, audience, expiry and revocation, then
re-resolves the user, customer binding and v2 permissions from the database.
"""

from dataclasses import dataclass
from typing import Protocol

import jwt
from pydantic import BaseModel

from rfq_copilot.api.auth import Principal
from rfq_copilot.mcp.client import McpCallError, McpToolClient


class McpCredentialProvider(Protocol):
    async def credential_for(self, principal: Principal) -> str:
        """A short-lived MCP-audience token for ``principal.subject``; raise to refuse."""


@dataclass(frozen=True)
class McpIntegration:
    client: McpToolClient
    credentials: McpCredentialProvider
    mcp_audience: str  # RFQ_MCP_JWT_AUDIENCE; must differ from the REST audience
    rest_audience: str

    def __post_init__(self) -> None:
        if self.credentials is None or not callable(
            getattr(self.credentials, "credential_for", None)
        ):
            raise TypeError("A trusted MCP credential provider is required.")
        if not self.mcp_audience or self.mcp_audience == self.rest_audience:
            raise ValueError("The MCP audience must be set and differ from the REST audience.")

    async def call(
        self, principal: Principal, tool: str, request: BaseModel | dict, *, rest_credential: str
    ) -> BaseModel:
        if not principal.subject:
            raise McpCallError("unauthenticated")
        try:
            token = await self.credentials.credential_for(principal)
        except Exception:
            raise McpCallError("unauthenticated") from None  # provider refused or failed
        self._check(token, principal, rest_credential)
        return await self.client.call(tool, request, token=token)

    def _check(self, token: str, principal: Principal, rest_credential: str) -> None:
        if not isinstance(token, str) or token == rest_credential:
            raise McpCallError("unauthenticated")  # never pass the REST token through
        try:
            claims = jwt.decode(token, options={"verify_signature": False})  # shape only
        except jwt.PyJWTError:
            raise McpCallError("unauthenticated") from None
        if claims.get("aud") != self.mcp_audience or claims.get("sub") != principal.subject:
            raise McpCallError("unauthenticated")  # wrong audience or another identity
