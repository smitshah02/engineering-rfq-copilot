"""Typed API settings from ``RFQ_API_*`` process variables.

Deliberately separate from the database configuration: no dotenv file is read, so the
API never reinterprets ``DATABASE_URL`` and starts without database credentials.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENV_PREFIX = "RFQ_API_"

_ClaimText = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[\x21-\x7e]+$")]
_KeyId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
MAX_TRUSTED_KEYS = 4


class ApiSettings(BaseModel):
    """Validated, immutable API settings. Unknown ``RFQ_API_*`` variables are rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Secure default: interactive docs and the OpenAPI schema are served only when a
    # deployment explicitly opts into development.
    environment: Literal["development", "production"] = "production"

    # Attachment uploads answer 503 until a storage directory is configured (absolute path;
    # created with mode 0700 if missing). Files get server-generated names, mode 0600.
    attachment_dir: Path | None = None
    # Largest accepted attachment. The multipart body is capped at this plus a small
    # allowance for part headers, while it streams, before anything is buffered whole.
    max_upload_bytes: Annotated[int, Field(ge=1, le=50 * 1024 * 1024)] = 10 * 1024 * 1024

    # Bearer-token trust boundary: one external issuer, one audience, one asymmetric
    # algorithm and one public key. All four or none; with none, a request carrying a
    # bearer token answers 503 (authentication unavailable) and nothing is authorized.
    # HMAC algorithms are excluded: this service verifies tokens, it never mints them.
    jwt_issuer: _ClaimText | None = None
    jwt_audience: _ClaimText | None = None
    jwt_algorithm: Literal["RS256", "ES256", "EdDSA"] | None = None
    jwt_public_key_file: Path | None = None
    # Rotation (TD-013), instead of jwt_public_key_file: 1 to 4 trusted keys named by key
    # id, "kid=/abs/key.pem,kid2=/abs/next.pem" in the environment. Each token's header
    # ``kid`` must equal one configured id exactly; there is no fallback and no fetching.
    # A key removed here and restarted is no longer trusted.
    jwt_public_keys: tuple[tuple[_KeyId, Path], ...] | None = None
    # Longest accepted exp - iat. Tokens with a longer declared lifetime are rejected.
    jwt_max_lifetime_seconds: Annotated[int, Field(ge=60, le=3600)] = 900

    # Phase 3.5 authorization foundations. Every default keeps the approved behaviour; each
    # other value needs head 20261111_06 applied to the target database (and, for v2, the
    # rbac-policy-v2 overlay) and fails closed (403/401/503) without it.
    # v1: catalog, quotes and uploads use view_assigned_rfqs / create_rfq (current grants).
    # v2: they use view_catalog / view_quote / attach_rfq_document (rbac-policy-v2 overlay).
    permission_policy: Literal["v1", "v2"] = "v1"
    # required: RFQ-scoped reads and uploads also need an active row in
    # business.rfq_assignments for the caller (revision 20261111_02).
    rfq_assignments: Literal["off", "required"] = "off"
    # enforced: every verified token is checked against business.token_revocations
    # (revision 20261111_03); unknown status is 503, never "not revoked".
    token_revocation: Literal["off", "enforced"] = "off"
    # selectable: a user with several active bindings names one per request in the
    # X-RFQ-Customer-Context header, checked against those bindings (deferred revision 20261111_05).
    customer_context: Literal["single", "selectable"] = "single"
    # enabled: uploads accept an Idempotency-Key header recorded in
    # business.idempotency_records (revision 20261111_04).
    upload_idempotency: Literal["off", "enabled"] = "off"

    @field_validator("attachment_dir", "jwt_public_key_file")
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("Path settings must be absolute.")
        return value

    @field_validator("jwt_public_keys", mode="before")
    @classmethod
    def _parse_key_set(cls, value: object) -> object:
        if isinstance(value, str):
            entries = []
            for item in value.split(","):
                kid, separator, path = item.strip().partition("=")
                if not separator:
                    raise ValueError("Key set entries must be kid=/absolute/path.")
                entries.append((kid, path))
            return tuple(entries)
        return value

    @field_validator("jwt_public_keys")
    @classmethod
    def _check_key_set(cls, value: tuple | None) -> tuple | None:
        if value is None:
            return None
        kids, paths = [kid for kid, _ in value], [path for _, path in value]
        if not 1 <= len(value) <= MAX_TRUSTED_KEYS:
            raise ValueError("Key set must hold 1 to 4 keys.")
        if len(set(kids)) != len(kids) or len(set(paths)) != len(paths):
            raise ValueError("Key ids and key files must be distinct.")
        if not all(path.is_absolute() for path in paths):
            raise ValueError("Path settings must be absolute.")
        return value

    @model_validator(mode="after")
    def _token_settings_complete(self) -> "ApiSettings":
        if self.jwt_public_key_file is not None and self.jwt_public_keys is not None:
            raise ValueError("Configure one key file or a key set, not both.")
        key = self.jwt_public_key_file or self.jwt_public_keys
        values = (self.jwt_issuer, self.jwt_audience, self.jwt_algorithm, key)
        if any(v is not None for v in values) and any(v is None for v in values):
            raise ValueError("JWT settings must be given together or not at all.")
        return self

    @property
    def authentication_configured(self) -> bool:
        return self.jwt_issuer is not None

    @property
    def docs_enabled(self) -> bool:
        return self.environment == "development"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ApiSettings":
        """Build settings from ``RFQ_API_<FIELD>`` variables; raises ``ValidationError``."""
        source = os.environ if environ is None else environ
        values = {
            key.removeprefix(ENV_PREFIX).lower(): value
            for key, value in source.items()
            if key.startswith(ENV_PREFIX)
        }
        return cls(**values)
