"""Redis client factory (cache, rate limiting, idempotency fast-path, velocity counters)."""

from functools import lru_cache

from redis import Redis

from pbip_api.core.config import get_settings


@lru_cache
def get_redis() -> Redis:
    settings = get_settings()
    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    if settings.otel_enabled:
        # Command names only, never keys (ADR-0050); imported lazily like core/db.get_engine.
        from pbip_api.core.tracing import instrument_redis

        instrument_redis(client, settings)
    return client
