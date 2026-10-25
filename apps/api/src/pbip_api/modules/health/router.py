"""Health endpoints used by Docker, CI smoke tests and Prometheus blackbox checks."""

import threading
import time
from collections.abc import Callable
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
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


class ReadinessCache:
    """Reuse the last readiness result for ``ttl_seconds``; one thread refreshes at a time.

    The probe is unauthenticated, so without this every request would cost a DB and a Redis round
    trip. Orchestrators poll every few seconds, so a result up to ``ttl_seconds`` old is fine.
    """

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._value: dict[str, CheckResult] | None = None
        self._expires_at = 0.0

    def get(self, compute: Callable[[], dict[str, CheckResult]]) -> dict[str, CheckResult]:
        if self.ttl_seconds <= 0:
            return compute()
        with self._lock:  # single flight: concurrent callers wait for one refresh (~ms)
            if self._value is None or self._clock() >= self._expires_at:
                self._value = compute()
                self._expires_at = self._clock() + self.ttl_seconds
            return self._value


def get_readiness_cache(request: Request) -> ReadinessCache:
    cache = getattr(request.app.state, "readiness_cache", None)
    if not isinstance(cache, ReadinessCache):
        raise RuntimeError("readiness cache not configured (see create_app)")
    return cache


@router.get("/live", response_model=LiveResponse, summary="Liveness probe")
async def live() -> LiveResponse:
    # async: no I/O here, so it should not occupy a threadpool slot.
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
    cache: ReadinessCache = Depends(get_readiness_cache),
) -> ReadyResponse:
    checks = cache.get(
        lambda: {"database": check_database(engine), "redis": check_redis(redis_client)}
    )
    healthy = all(result == "ok" for result in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="ok" if healthy else "degraded", checks=checks)
