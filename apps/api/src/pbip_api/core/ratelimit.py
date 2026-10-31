"""Fixed-window rate limiting on Redis.

Fails OPEN if Redis is unreachable (availability over strictness) - the database-backed
account lockout remains the brute-force backstop. The failure is logged and metered.
"""

from collections.abc import Callable, Iterable

import structlog
from fastapi import Depends, Request
from redis import Redis
from redis.exceptions import RedisError

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.errors import RateLimitedError
from pbip_api.core.redis import get_redis

_logger = structlog.get_logger(__name__)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit_key(name: str, ip: str) -> str:
    return f"rl:{name}:{ip}"


def clear_rate_limits(redis_client: Redis, names: Iterable[str], ips: Iterable[str]) -> int | None:
    """Delete the fixed-window counters for every (limiter, ip) pair.

    Returns the number of counters removed, or ``None`` if Redis is unreachable: callers treat
    clearing as best effort because the windows expire on their own.
    """
    keys = [rate_limit_key(name, ip) for name in names for ip in ips]
    if not keys:
        return 0
    try:
        return int(redis_client.delete(*keys))
    except RedisError as exc:
        _logger.warning("rate_limit_clear_unavailable", error_type=type(exc).__name__)
        return None


def consume(redis_client: Redis, name: str, subject: str, limit: int, window_seconds: int) -> None:
    """Count one call for ``subject`` (an IP, or ``user:<id>``); raise once over ``limit``."""
    key = rate_limit_key(name, subject)
    try:
        count = int(redis_client.incr(key))
        if count == 1:
            redis_client.expire(key, window_seconds)
    except RedisError as exc:
        _logger.warning("rate_limit_unavailable", name=name, error_type=type(exc).__name__)
        return
    if count > limit:
        raise RateLimitedError(
            "too many requests, try again later",
            headers={"Retry-After": str(window_seconds)},
        )


def rate_limit(
    name: str, limit: Callable[[Settings], int], window_seconds: int
) -> Callable[..., None]:
    """Build a dependency allowing ``limit(settings)`` calls per window per client IP."""

    def dependency(
        request: Request,
        redis_client: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings),
    ) -> None:
        consume(redis_client, name, client_ip(request), limit(settings), window_seconds)

    dependency.__name__ = f"rate_limit_{name}"
    return dependency
