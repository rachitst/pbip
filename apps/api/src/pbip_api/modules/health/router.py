"""Health endpoints used by Docker, CI smoke tests and Prometheus blackbox checks."""

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.concurrency import run_in_threadpool
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
        # One tuple, replaced atomically, so fresh() can read it without the lock.
        self._entry: tuple[dict[str, CheckResult], float] | None = None
        self._refresh: asyncio.Future[dict[str, CheckResult]] | None = None

    def fresh(self) -> dict[str, CheckResult] | None:
        """The cached result if still valid, else None. Never blocks (safe on the event loop)."""
        entry = self._entry
        if self.ttl_seconds <= 0 or entry is None or self._clock() >= entry[1]:
            return None
        return entry[0]

    def get(self, compute: Callable[[], dict[str, CheckResult]]) -> dict[str, CheckResult]:
        if self.ttl_seconds <= 0:
            return compute()
        with self._lock:  # single flight: concurrent callers wait for one refresh (~ms)
            value = self.fresh()
            if value is None:
                value = compute()
                self._entry = (value, self._clock() + self.ttl_seconds)
            return value

    async def get_async(
        self, compute: Callable[[], dict[str, CheckResult]]
    ) -> dict[str, CheckResult]:
        """Cache hit: no thread hop. Miss: ONE thread-pool refresh that concurrent probes share.

        Without the shared task every probe arriving at expiry took its own hop (50 at 50 VUs),
        which alone pushed p95 past a second on a 2-core Windows host (#134).
        """
        value = self.fresh()
        if value is not None:
            return value
        refresh = self._refresh
        if (
            refresh is None
            or refresh.done()
            or refresh.get_loop() is not asyncio.get_running_loop()
        ):
            refresh = asyncio.ensure_future(run_in_threadpool(self.get, compute))
            self._refresh = refresh
        return await asyncio.shield(refresh)


async def get_readiness_cache(request: Request) -> ReadinessCache:
    # async: a sync dependency would cost a thread-pool hop on every probe.
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
async def ready(
    request: Request,
    response: Response,
    cache: ReadinessCache = Depends(get_readiness_cache),
) -> ReadyResponse:
    # async with a lock-free cache hit: no thread-pool hop (#134). A sync handler plus the sync
    # engine/redis dependencies cost four hops per probe, which pushed p95 over 50 ms at 50 VUs
    # on a 2-core host. get_engine/get_redis are resolved only on a miss, inside the one shared
    # refresh, honouring dependency_overrides.
    overrides = request.app.dependency_overrides

    def probe() -> dict[str, CheckResult]:
        engine: Engine = overrides.get(get_engine, get_engine)()
        redis_client: Redis = overrides.get(get_redis, get_redis)()
        return {"database": check_database(engine), "redis": check_redis(redis_client)}

    checks = await cache.get_async(probe)
    healthy = all(result == "ok" for result in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="ok" if healthy else "degraded", checks=checks)
