"""Health endpoints used by Docker, CI smoke tests and Prometheus blackbox checks."""

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from redis import Redis
from sqlalchemy import Engine, text

from pbip_api.core.db import get_engine
from pbip_api.core.redis import get_redis

router = APIRouter(prefix="/api/health", tags=["health"])
_logger = structlog.get_logger(__name__)

CheckResult = Literal["ok", "fail"]


class LiveResponse(BaseModel):
    status: Literal["ok"]


class ReadyResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, CheckResult]


def check_database(engine: Engine) -> CheckResult:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        _logger.warning("readiness_check_failed", check="database", error_type=type(exc).__name__)
        return "fail"
    return "ok"


def check_redis(client: Redis) -> CheckResult:
    try:
        client.ping()
    except Exception as exc:
        _logger.warning("readiness_check_failed", check="redis", error_type=type(exc).__name__)
        return "fail"
    return "ok"


@router.get("/live", response_model=LiveResponse, summary="Liveness probe")
def live() -> LiveResponse:
    return LiveResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Readiness probe (database and Redis reachable)",
    responses={503: {"model": ReadyResponse}},
)
def ready(
    response: Response,
    engine: Engine = Depends(get_engine),
    redis_client: Redis = Depends(get_redis),
) -> ReadyResponse:
    checks: dict[str, CheckResult] = {
        "database": check_database(engine),
        "redis": check_redis(redis_client),
    }
    healthy = all(result == "ok" for result in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="ok" if healthy else "degraded", checks=checks)
