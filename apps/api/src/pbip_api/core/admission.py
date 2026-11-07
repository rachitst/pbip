"""Database admission control (TASK-079, ADR-0061): no more requests in flight than connections."""

import asyncio

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from pbip_api.core.errors import problem
from pbip_api.core.metrics import DB_ADMISSION_REJECTED_TOTAL

_logger = structlog.get_logger(__name__)


class DatabaseAdmissionMiddleware:
    """Lets at most ``limit`` requests into the app at once: the size of the connection pool.

    FastAPI runs each sync dependency, the endpoint and the response validation as separate
    worker-thread calls, and a request keeps its database connection between them. With more
    requests in flight than connections, requests held connections while waiting for a worker
    thread while every worker thread waited for a connection: nothing moved until the pool
    timeout. A request uses at most one connection, so with no more requests inside than
    connections a checkout never waits and that cycle cannot form. The others wait here, on the
    event loop, holding neither a thread nor a connection; after ``wait_seconds`` they get a 503.

    ``exempt_paths`` are routes that never touch the database (liveness, the metrics scrape).
    """

    def __init__(
        self, app: ASGIApp, limit: int, wait_seconds: float, exempt_paths: frozenset[str]
    ) -> None:
        self.app = app
        self.limit = limit
        self.wait_seconds = wait_seconds
        self.exempt_paths = exempt_paths
        self._slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        # One per event loop: an asyncio.Semaphore is bound to the loop it first waits on, and a
        # process serves from one loop (tests start a new loop per TestClient).
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots[0] is not loop:
            self._slots = (loop, asyncio.Semaphore(self.limit))
        return self._slots[1]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        slots = self._semaphore()
        try:
            async with asyncio.timeout(self.wait_seconds):
                await slots.acquire()
        except TimeoutError:
            DB_ADMISSION_REJECTED_TOTAL.inc()
            _logger.warning("db_admission_rejected", wait_seconds=self.wait_seconds)
            busy = problem(
                Request(scope),
                503,
                "service_busy",
                "The service is busy. Please try again shortly.",
                headers={"Retry-After": "1"},
            )
            await busy(scope, receive, send)
            return
        try:
            # Held until the app returns, which is after FastAPI has closed the request's session
            # (dependency teardown runs after the response, streamed or not, is sent).
            await self.app(scope, receive, send)
        finally:
            slots.release()
