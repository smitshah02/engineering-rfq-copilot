"""Typed API settings from ``RFQ_API_*`` process variables.

Deliberately separate from the database configuration: no dotenv file is read, so the
API never reinterprets ``DATABASE_URL`` and starts without database credentials.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ENV_PREFIX = "RFQ_API_"


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

    @field_validator("attachment_dir")
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("attachment_dir must be an absolute path.")
        return value

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
