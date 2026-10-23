"""Liveness and readiness probes.

Liveness answers "is the process alive?" and must not touch dependencies: if Postgres is
down, restarting the API would not help, so the orchestrator should not kill it.
Readiness answers "should I receive traffic?" and does check dependencies, so a load
balancer stops routing to an instance that cannot serve.

Only Postgres is required for readiness. Redis is reported but does not fail the probe:
the service degrades gracefully without it, and since every instance shares the same Redis,
failing readiness on it would pull *all* instances out of the load balancer at once and turn
a cache outage into a full outage.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import RedisDep, SessionDep

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


class LivenessResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, str]


@router.get("/healthz", response_model=LivenessResponse)
async def healthz() -> LivenessResponse:
    """Always 200 while the event loop is responsive."""
    return LivenessResponse(status="ok")


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readyz(session: SessionDep, redis: RedisDep) -> JSONResponse:
    """200 when Postgres answers (``degraded`` if Redis does not); 503 otherwise."""
    checks: dict[str, str] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("readiness_database_failed", extra={"error": repr(exc)})
        checks["database"] = "unavailable"
    checks["redis"] = "ok" if await redis.ping() else "unavailable"

    if checks["database"] != "ok":
        overall, status_code = "unavailable", 503
    elif checks["redis"] != "ok":
        overall, status_code = "degraded", 200
    else:
        overall, status_code = "ok", 200
    body = ReadinessResponse(status=overall, checks=checks)
    return JSONResponse(status_code=status_code, content=body.model_dump())
