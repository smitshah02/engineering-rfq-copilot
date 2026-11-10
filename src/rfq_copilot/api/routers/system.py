"""Non-sensitive service metadata under the versioned API prefix."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

API_VERSION: Literal["v1"] = "v1"

router = APIRouter(prefix="/system", tags=["system"])


class SystemInfo(BaseModel):
    """Public contract: name and versions only, never configuration or infrastructure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    api_version: Literal["v1"]


def get_system_info(request: Request) -> SystemInfo:
    """Dependency: metadata of the application serving this request (overridable in tests)."""
    return SystemInfo(name=request.app.title, version=request.app.version, api_version=API_VERSION)


@router.get("/info", summary="Service name and versions")
async def info(system_info: Annotated[SystemInfo, Depends(get_system_info)]) -> SystemInfo:
    return system_info
