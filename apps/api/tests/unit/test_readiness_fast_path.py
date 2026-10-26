"""/api/health/ready answers cache hits without any thread-pool hop (#134).

The sync handler plus sync engine/redis/cache dependencies cost four thread-pool hops per probe,
which made /ready 3-4x slower than /live and pushed the k6 baseline's p95 past 50 ms on a 2-core
Windows host. A cache hit must now cost no hop, and a miss exactly one shared refresh.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient
from pbip_api.modules.health import router as health
from pbip_api.modules.health.router import CheckResult, ReadinessCache
from pbip_test_support import build_app

import scripts.dev as dev_mod

pytestmark = pytest.mark.unit


def test_ready_and_its_dependencies_are_coroutines() -> None:
    assert inspect.iscoroutinefunction(health.ready)
    assert inspect.iscoroutinefunction(health.get_readiness_cache)
    params = inspect.signature(health.ready).parameters
    assert set(params) == {"request", "response", "cache"}  # no sync engine/redis dependencies


def test_cache_hits_take_no_thread_pool_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    hops: list[str] = []

    async def counting(func: Callable[..., Any], *args: Any) -> Any:
        hops.append(getattr(func, "__name__", "?"))
        return await run_in_threadpool(func, *args)

    monkeypatch.setattr("pbip_api.modules.health.router.run_in_threadpool", counting)
    with TestClient(build_app()) as client:
        for _ in range(20):
            assert client.get("/api/health/ready").json()["status"] == "ok"
    assert hops == ["get"]  # one refresh for 20 probes within the TTL


def test_concurrent_misses_share_one_refresh() -> None:
    now = [100.0]
    cache = ReadinessCache(1.0, clock=lambda: now[0])
    calls: list[int] = []

    def compute() -> dict[str, CheckResult]:
        calls.append(1)
        return {"database": "ok", "redis": "ok"}

    async def burst() -> list[dict[str, CheckResult]]:
        return await asyncio.gather(*(cache.get_async(compute) for _ in range(50)))

    assert all(r == {"database": "ok", "redis": "ok"} for r in asyncio.run(burst()))
    assert len(calls) == 1
    now[0] += 1.0  # expired: the next burst refreshes once more, never serving the stale value
    asyncio.run(burst())
    assert len(calls) == 2


def test_fresh_never_returns_an_expired_or_disabled_result() -> None:
    now = [0.0]
    cache = ReadinessCache(1.0, clock=lambda: now[0])
    assert cache.fresh() is None
    cache.get(lambda: {"database": "ok", "redis": "ok"})
    assert cache.fresh() == {"database": "ok", "redis": "ok"}
    now[0] = 1.0
    assert cache.fresh() is None
    assert ReadinessCache(0).fresh() is None


def test_uvicorn_does_not_write_a_second_access_log() -> None:
    """The app logs one structured access line without the query string (AGENTS.md section 5);
    uvicorn's own access line duplicated it with the raw query string."""
    seen: list[list[str]] = []

    def record(cmd: list[str], **_kwargs: object) -> int:
        seen.append(cmd)
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dev_mod, "_run", record)
        dev_mod.cmd_dev_api([])
    assert "--no-access-log" in seen[0]
    dockerfile = (dev_mod.ROOT / "apps" / "api" / "Dockerfile").read_text(encoding="utf-8")
    cmd_line = next(ln for ln in dockerfile.splitlines() if ln.startswith("CMD"))
    assert '"--no-access-log"' in cmd_line
