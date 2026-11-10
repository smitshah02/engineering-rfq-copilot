"""Process liveness. Checks nothing downstream: not PostgreSQL, not the dataset."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/health", tags=["health"])


class Liveness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["alive"]


@router.get("/live", summary="Process liveness")
async def live() -> Liveness:
    """Return 200 while the process can serve requests. Not a readiness check."""
    return Liveness(status="alive")
