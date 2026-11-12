"""MCP authentication: an SDK ``TokenVerifier`` built on the existing JWT verifier.

The MCP boundary is independent of FastAPI. A bearer token is accepted only if:
1. ``api.tokens.TokenVerifier`` verifies it: approved asymmetric algorithm and key/kid,
   issuer, the **MCP audience** (a REST token, whose ``aud`` is the REST audience, fails),
   exp/nbf/iat and maximum lifetime, a well-formed ``sub``.
2. ``api.auth.resolve_identity`` (shared with REST) finds an active user, the token is not
   revoked (**always enforced** for MCP), the user has exactly one eligible customer
   binding, and reads the role's grants from ``business.role_permissions``.

The result is an ``McpPrincipal``: the SDK ``AccessToken`` carrying the verified customer,
user and permissions. The SDK keeps it in a request-scoped context variable
(``get_access_token``); nothing is stored globally. Identity reads use a separate
infrastructure session, never the business evaluation snapshot.
"""

import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.api.auth import resolve_identity
from rfq_copilot.api.tokens import InvalidToken, TokenVerifier, load_public_key

ENV_PREFIX = "RFQ_MCP_"
logger = logging.getLogger(__name__)


class McpAuthSettings(BaseModel):
    """MCP-only settings. Policy v2 and enforced revocation are the only accepted values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # RFC 9728 metadata identifier for one endpoint. NOT a token binding: tokens are bound
    # to ``jwt_audience``, the whole MCP host (see ``auth_settings``).
    resource_server_url: AnyHttpUrl
    jwt_issuer: Annotated[str, Field(min_length=1, max_length=255)]
    jwt_audience: Annotated[str, Field(min_length=1, max_length=255)]  # dedicated to MCP
    jwt_algorithm: Literal["RS256", "ES256", "EdDSA"]
    jwt_public_key_file: Path
    jwt_max_lifetime_seconds: Annotated[int, Field(ge=60, le=3600)] = 900
    permission_policy: Literal["v2"] = "v2"  # no v1 fallback exists for MCP (F1)
    token_revocation: Literal["enforced"] = "enforced"  # never inherits the REST "off" (F2)
    rfq_assignments: Literal["off", "required"] = "off"
    rest_audience: str | None = None  # the REST API's audience, which must differ

    @model_validator(mode="after")
    def _dedicated_audience(self) -> "McpAuthSettings":
        if self.rest_audience is not None and self.jwt_audience == self.rest_audience:
            raise ValueError("The MCP audience must differ from the REST API audience.")
        if not self.jwt_public_key_file.is_absolute():
            raise ValueError("jwt_public_key_file must be an absolute path.")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "McpAuthSettings":
        # The host's own namespaces share the prefix; anything else unknown still fails
        # (a misspelled security setting must never be silently ignored).
        other = ("RFQ_MCP_HOST_", "RFQ_MCP_DATABASE_TARGET")
        values = {
            key.removeprefix(ENV_PREFIX).lower(): value
            for key, value in environ.items()
            if key.startswith(ENV_PREFIX) and not key.startswith(other)
        }
        return cls(rest_audience=environ.get("RFQ_API_JWT_AUDIENCE"), **values)

    def auth_settings(self) -> AuthSettings:
        # Day 30: one shared protected resource. The audience check in ``verify_token``
        # (strict ``aud`` == the MCP audience) is the binding, so the SDK's resource check
        # is off, as the SDK prescribes for verifiers that validate the audience themselves.
        # The previous True compared a value this verifier copied from its own settings, so
        # it could never fail and only looked like per-endpoint binding. Per-endpoint
        # binding needs an issuer that mints RFC 8707 per-resource audiences (none exists).
        return AuthSettings(
            issuer_url=self.jwt_issuer,
            resource_server_url=self.resource_server_url,
            validate_token_resource=False,
        )


class McpPrincipal(AccessToken):
    """A verified MCP caller. Built only by ``McpTokenVerifier``; tools trust nothing else."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    customer_id: str
    permissions: frozenset[str]
    assignment_required: bool

    @property
    def assigned_user(self) -> str | None:
        return self.user_id if self.assignment_required else None


IdentitySession = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class McpTokenVerifier:
    """SDK ``TokenVerifier``: returns an ``McpPrincipal`` or None (the SDK answers 401)."""

    def __init__(
        self, settings: McpAuthSettings, identity_session: IdentitySession, jwt: TokenVerifier
    ) -> None:
        if jwt.audience != settings.jwt_audience or jwt.issuer != settings.jwt_issuer:
            raise ValueError("The JWT verifier must use the MCP issuer and audience.")
        self.settings, self._identity_session, self._jwt = settings, identity_session, jwt

    @classmethod
    def from_settings(
        cls, settings: McpAuthSettings, identity_session: IdentitySession
    ) -> "McpTokenVerifier":
        jwt = TokenVerifier(
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            algorithm=settings.jwt_algorithm,
            key=load_public_key(settings.jwt_public_key_file, settings.jwt_algorithm),
            max_lifetime_seconds=settings.jwt_max_lifetime_seconds,
        )
        return cls(settings, identity_session, jwt)

    async def verify_token(self, token: str) -> McpPrincipal | None:
        try:
            verified = self._jwt.verify(token)
        except InvalidToken:
            return None
        try:
            async with self._identity_session() as session:
                user, customer_id, granted = await resolve_identity(
                    session,
                    token,
                    verified,
                    token_revocation=self.settings.token_revocation,
                    customer_context="single",  # no customer selection over MCP
                )
        except Exception as exc:
            # Identity or revocation status unknown: fail closed, never allow. The type
            # only: no token, subject or driver detail is logged.
            logger.warning("MCP identity resolution failed: %s", type(exc).__name__)
            return None
        if user is None or customer_id is None or granted is None:
            return None
        return McpPrincipal(
            token=token,
            client_id=user.user_id,
            subject=verified.subject,
            scopes=sorted(granted),
            expires_at=None,
            resource=self.settings.jwt_audience,  # what the issuer bound: the verified aud
            user_id=user.user_id,
            customer_id=customer_id,
            permissions=frozenset(granted),
            assignment_required=self.settings.rfq_assignments == "required",
        )
