"""TASK-079 / ADR-0061: no more requests inside the app than the pool has connections."""

import asyncio
import json
from typing import Any, cast

import pytest
from pbip_api.core.admission import DatabaseAdmissionMiddleware
from pbip_api.core.config import Settings
from pbip_api.core.metrics import DB_ADMISSION_REJECTED_TOTAL
from pbip_api.main import create_app
from starlette.types import Message, Receive, Scope, Send

pytestmark = pytest.mark.unit


class GatedApp:
    """An ASGI app that stays 'inside' until its gate opens, and counts who is inside."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.inside = 0
        self.peak = 0
        self.entered = asyncio.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.inside += 1
        self.peak = max(self.peak, self.inside)
        self.entered.set()
        try:
            if scope["path"] == "/boom":
                raise RuntimeError("app failed")
            await self.gate.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        finally:
            self.inside -= 1


async def _call(app: DatabaseAdmissionMiddleware, path: str) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "pbip_request_id": "req-12345678",
    }
    await app(scope, receive, send)
    return sent


def _status(messages: list[Message]) -> int:
    return int(messages[0]["status"])


def _middleware(inner: GatedApp, limit: int, wait: float) -> DatabaseAdmissionMiddleware:
    return DatabaseAdmissionMiddleware(
        inner, limit=limit, wait_seconds=wait, exempt_paths=frozenset({"/api/health/live"})
    )


def test_requests_beyond_the_limit_wait_outside_and_enter_in_turn() -> None:
    async def scenario() -> tuple[list[int], int]:
        inner = GatedApp()
        app = _middleware(inner, limit=2, wait=30)
        tasks = [asyncio.create_task(_call(app, "/api/v1/accounts")) for _ in range(5)]
        while inner.inside < 2:
            await asyncio.sleep(0)
        assert inner.inside == 2  # three are waiting outside, holding no thread or connection
        inner.gate.set()
        results = await asyncio.gather(*tasks)
        return [_status(r) for r in results], inner.peak

    statuses, peak = asyncio.run(scenario())
    assert statuses == [200] * 5
    assert peak == 2


def test_a_request_that_waits_too_long_gets_a_503_problem() -> None:
    async def scenario() -> list[Message]:
        inner = GatedApp()
        app = _middleware(inner, limit=1, wait=0.05)
        holder = asyncio.create_task(_call(app, "/api/v1/accounts"))
        await inner.entered.wait()
        rejected = await _call(app, "/api/v1/accounts")
        inner.gate.set()
        await holder
        return rejected

    before = DB_ADMISSION_REJECTED_TOTAL._value.get()  # no public accessor on a Counter
    messages = asyncio.run(scenario())
    assert _status(messages) == 503
    headers = {k.decode(): v.decode() for k, v in messages[0]["headers"]}
    assert headers["content-type"] == "application/problem+json"
    assert headers["retry-after"] == "1"
    body = json.loads(messages[1]["body"])
    assert body["code"] == "service_busy" and body["status"] == 503
    assert body["request_id"] == "req-12345678"
    assert DB_ADMISSION_REJECTED_TOTAL._value.get() == before + 1


def test_a_failing_request_gives_its_slot_back() -> None:
    async def scenario() -> int:
        inner = GatedApp()
        inner.gate.set()
        app = _middleware(inner, limit=1, wait=0.05)
        with pytest.raises(RuntimeError):
            await _call(app, "/boom")
        return _status(await _call(app, "/api/v1/accounts"))

    assert asyncio.run(scenario()) == 200


def test_exempt_paths_bypass_a_full_house() -> None:
    async def scenario() -> int:
        inner = GatedApp()
        app = _middleware(inner, limit=1, wait=0.05)
        holder = asyncio.create_task(_call(app, "/api/v1/accounts"))
        await inner.entered.wait()
        live = asyncio.create_task(_call(app, "/api/health/live"))
        while inner.inside < 2:
            await asyncio.sleep(0)
        inner.gate.set()
        await holder
        return _status(await live)

    assert asyncio.run(scenario()) == 200


def test_each_event_loop_gets_its_own_slots() -> None:
    """Tests start a new loop per TestClient; a semaphore from an old loop must not leak in."""
    inner = GatedApp()
    inner.gate.set()
    app = _middleware(inner, limit=1, wait=0.05)
    assert _status(asyncio.run(_call(app, "/api/v1/accounts"))) == 200
    assert _status(asyncio.run(_call(app, "/api/v1/accounts"))) == 200


def test_the_app_admits_as_many_requests_as_the_pool_has_connections() -> None:
    app = create_app(Settings(db_pool_size=7, db_max_overflow=3, db_pool_timeout_seconds=4.5))
    [admission] = [
        m for m in app.user_middleware if cast(object, m.cls) is DatabaseAdmissionMiddleware
    ]
    assert admission.kwargs == {
        "limit": 10,
        "wait_seconds": 4.5,
        "exempt_paths": frozenset({"/api/health/live", "/metrics"}),
    }
    # Innermost (added first), so a 503 still passes the request-id and security-header layers.
    assert cast(object, app.user_middleware[-1].cls) is DatabaseAdmissionMiddleware
