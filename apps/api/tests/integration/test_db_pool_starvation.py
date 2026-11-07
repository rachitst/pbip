"""TASK-079: requests must not starve the database connection pool (ADR-0060).

The staging load test collapsed at 200 VUs with idle CPU and ``QueuePool limit ... timed out``.
Two mechanisms were proven here, against the real PostgreSQL with a scaled-down pool:

* FastAPI runs every sync dependency, the endpoint and the response validation as separate
  worker-thread calls. The request session checks a connection out in the auth dependency and
  keeps it between those calls, so a request can hold a connection while it waits for a worker
  thread, while the threads are all blocked waiting for a connection. Nothing moves until
  ``pool_timeout``.
* The SSE assistant stream opened a second session while the request's session still held its
  connection, so one stream needed two connections at once.

The production numbers are 40 worker threads and a pool of 10 + 10 per API process; these tests
keep the same shape at 2 threads and a pool of 1 so that the cycle is reached with a handful of
requests instead of hundreds.
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import anyio.to_thread
import pytest
from fakeredis import FakeRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_engine
from pbip_api.core.redis import get_redis
from pbip_api.main import create_app
from pbip_api.modules.assistant.dependencies import get_chat_model
from pbip_api.modules.auth.dependencies import get_clock
from pbip_test_support import Actor, FakeClock, age_payee
from sqlalchemy import Engine, create_engine, event, text

pytestmark = pytest.mark.integration

# Also the admission wait (ADR-0060): long enough for every queued request on a slow CI runner.
POOL_TIMEOUT_SECONDS = 5.0
WORKER_THREADS = 2


class PoolProbe:
    """Counts connections checked out of one engine's pool, and the peak since the last reset."""

    def __init__(self, engine: Engine) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0
        event.listen(engine.pool, "checkout", self._checkout)
        event.listen(engine.pool, "checkin", self._checkin)

    def _checkout(self, *_: object) -> None:
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def _checkin(self, *_: object) -> None:
        with self._lock:
            self.current -= 1

    def reset(self) -> None:
        with self._lock:
            self.peak = self.current


@dataclass
class TinyStack:
    client: TestClient
    engine: Engine
    probe: PoolProbe
    app: FastAPI


@contextmanager
def _stack(
    url: str, settings: Settings, redis: FakeRedis, clock: FakeClock, pool_size: int
) -> Iterator[TinyStack]:
    """The API on its own pool of ``pool_size`` connections and two worker threads.

    Same JWT secret, Redis and clock as the ``client`` fixture, so its actors' tokens work here.
    """
    tuned = settings.model_copy(
        update={
            "db_pool_size": pool_size,
            "db_max_overflow": 0,
            "db_pool_timeout_seconds": POOL_TIMEOUT_SECONDS,
        }
    )
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=tuned.db_pool_size,
        max_overflow=tuned.db_max_overflow,
        pool_timeout=tuned.db_pool_timeout_seconds,
    )
    probe = PoolProbe(engine)
    application = create_app(tuned)
    application.dependency_overrides[get_engine] = lambda: engine
    application.dependency_overrides[get_settings] = lambda: tuned
    application.dependency_overrides[get_redis] = lambda: redis
    application.dependency_overrides[get_clock] = lambda: clock
    try:
        with TestClient(application, raise_server_exceptions=False) as client:

            async def limit_worker_threads() -> None:
                anyio.to_thread.current_default_thread_limiter().total_tokens = WORKER_THREADS

            client.portal.call(limit_worker_threads)  # type: ignore[union-attr]  # set by __enter__
            yield TinyStack(client, engine, probe, application)
    finally:
        engine.dispose()


@pytest.fixture
def tiny_stack(
    test_database_url: str, settings: Settings, fake_redis: FakeRedis, clock: FakeClock
) -> Iterator[TinyStack]:
    """One connection, two worker threads: production's 10 + 10 and 40, scaled down."""
    with _stack(test_database_url, settings, fake_redis, clock, pool_size=1) as stack:
        yield stack


@pytest.fixture
def roomy_stack(
    test_database_url: str, settings: Settings, fake_redis: FakeRedis, clock: FakeClock
) -> Iterator[TinyStack]:
    """Enough connections that a request needing a second one gets it and the probe counts it."""
    with _stack(test_database_url, settings, fake_redis, clock, pool_size=4) as stack:
        yield stack


def _events(body: str) -> list[str]:
    return [frame.split("\n")[0].removeprefix("event: ") for frame in body.strip().split("\n\n")]


def _transfer(client: TestClient, actor: Actor, payee_id: str, amount: int) -> Any:
    return client.post(
        "/api/v1/transfers",
        headers={**actor.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(actor.account_id),
            "amount_minor": amount,
            "payee_id": payee_id,
            "reference": "Pool starvation test",
        },
    )


def test_assistant_stream_needs_only_one_connection(
    tiny_stack: TinyStack, make_customer: Callable[..., Actor]
) -> None:
    """Regression: the stream opened its own session while the request's session held the only
    connection, waited ``pool_timeout`` for a second one and ended with an ``error`` event."""
    alice = make_customer(funds=5_000_00)
    tiny_stack.probe.reset()
    started = time.monotonic()
    resp = tiny_stack.client.post(
        "/api/v1/assistant/chat/stream", headers=alice.headers, json={"message": "my balance"}
    )
    assert resp.status_code == 200
    assert _events(resp.text)[-1] == "done", resp.text
    assert time.monotonic() - started < POOL_TIMEOUT_SECONDS
    assert tiny_stack.probe.peak == 1


class _PoolCheckingModel:
    """A chat model that records how many connections are checked out while it 'thinks'."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.checked_out_during_call: list[int] = []

    def complete_json(self, system: str, user: str, schema: Mapping[str, object]) -> str:
        self.checked_out_during_call.append(self.engine.pool.checkedout())  # type: ignore[attr-defined]  # QueuePool
        return '{"tool": "get_account_summary", "arguments": {}}'


@pytest.mark.parametrize("path", ["/api/v1/assistant/chat", "/api/v1/assistant/chat/stream"])
def test_no_connection_is_held_while_the_model_is_called(
    tiny_stack: TinyStack, make_customer: Callable[..., Actor], path: str
) -> None:
    """A model call takes seconds; a connection idle in a transaction meanwhile is wasted."""
    alice = make_customer(funds=5_000_00)
    model = _PoolCheckingModel(tiny_stack.engine)
    tiny_stack.app.dependency_overrides[get_chat_model] = lambda: model
    resp = tiny_stack.client.post(
        path, headers=alice.headers, json={"message": "am I doing ok money-wise?"}
    )
    assert resp.status_code == 200
    if path.endswith("stream"):
        assert _events(resp.text)[-1] == "done", resp.text
    else:
        assert resp.json()["engine"] == "llm", resp.text
    assert model.checked_out_during_call == [0]


def test_each_request_type_holds_at_most_one_connection(
    roomy_stack: TinyStack,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    """The admission limit (ADR-0060) is the pool size, which is only safe at <= 1 each."""
    alice = make_customer(funds=5_000_00)
    bob = make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    client, probe = roomy_stack.client, roomy_stack.probe
    requests: dict[str, Callable[[], Any]] = {
        "accounts": lambda: client.get("/api/v1/accounts", headers=alice.headers),
        "transactions": lambda: client.get(
            f"/api/v1/accounts/{alice.account_id}/transactions?limit=25", headers=alice.headers
        ),
        "spending": lambda: client.get(
            f"/api/v1/analytics/spending?account_id={alice.account_id}", headers=alice.headers
        ),
        "transfer": lambda: _transfer(client, alice, payee["id"], 1_00),
        "assistant_chat": lambda: client.post(
            "/api/v1/assistant/chat", headers=alice.headers, json={"message": "my balance"}
        ),
        "assistant_stream": lambda: client.post(
            "/api/v1/assistant/chat/stream", headers=alice.headers, json={"message": "my balance"}
        ),
    }
    peaks: dict[str, int] = {}
    for name, send in requests.items():
        probe.reset()
        resp = send()
        assert resp.status_code in (200, 201), (name, resp.text)
        if name == "assistant_stream":
            assert _events(resp.text)[-1] == "done", resp.text
        peaks[name] = probe.peak
    assert peaks == dict.fromkeys(requests, 1)


@pytest.mark.timeout(180)  # on main each stall lasted POOL_TIMEOUT_SECONDS, repeatedly
@pytest.mark.parametrize("with_assistant", [False, True], ids=["banking_only", "with_assistant"])
def test_concurrent_mixed_requests_do_not_starve_a_small_pool(
    with_assistant: bool,
    tiny_stack: TinyStack,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    """Real threads, more requests than worker threads and connections, every k6 route group.

    Before ADR-0060 requests held the one connection while waiting for a worker thread and the
    threads waited for the connection: requests failed after ``pool_timeout`` with a
    ``QueuePool`` timeout (500) or a stream ``error`` event. Same-user transfers also contend
    for the per-user advisory lock (AGENTS.md section 4). ``banking_only`` leaves the assistant
    out: the thread/connection cycle starved the pool without any request needing two connections.
    """
    customers = [make_customer(funds=1_000_00) for _ in range(3)]
    sink = make_customer()
    payees = [make_verified_payee(c, sink)["id"] for c in customers]
    for payee_id in payees:
        age_payee(migrated_engine, payee_id, 48)
    client = tiny_stack.client

    def calls(actor: Actor, payee_id: str) -> list[Callable[[], tuple[str, Any]]]:
        def get(path: str) -> Callable[[], tuple[str, Any]]:
            return lambda: (path, client.get(path, headers=actor.headers))

        def post(path: str, message: str) -> Callable[[], tuple[str, Any]]:
            return lambda: (
                path,
                client.post(path, headers=actor.headers, json={"message": message}),
            )

        banking = [
            get("/api/v1/accounts"),
            get(f"/api/v1/accounts/{actor.account_id}/transactions?limit=25"),
            get(f"/api/v1/analytics/spending?account_id={actor.account_id}"),
            lambda: ("transfer", _transfer(client, actor, payee_id, 10_00)),
            lambda: ("transfer", _transfer(client, actor, payee_id, 10_00)),
        ]
        if not with_assistant:
            return banking
        return [
            *banking,
            post("/api/v1/assistant/chat", "my balance"),
            post("/api/v1/assistant/chat/stream", "my balance"),
        ]

    work = [
        call for actor, payee in zip(customers, payees, strict=True) for call in calls(actor, payee)
    ]
    barrier = threading.Barrier(len(work))

    def run(call: Callable[[], tuple[str, Any]]) -> tuple[str, int, str]:
        barrier.wait()
        name, resp = call()
        return name, resp.status_code, resp.text

    with ThreadPoolExecutor(max_workers=len(work)) as pool:
        results = list(pool.map(run, work))

    # A stall ends as a QueuePool timeout (500), an admission timeout (503) or a stream error.
    failures = [r for r in results if r[1] not in (200, 201)]
    failures += [r for r in results if r[0].endswith("stream") and _events(r[2])[-1] != "done"]
    assert failures == []
    # Money invariant: every customer paid exactly two transfers, nothing more, nothing less.
    with migrated_engine.connect() as connection:
        balances = connection.execute(
            text("SELECT id, ledger_balance FROM accounts WHERE id = ANY(:ids)"),
            {"ids": [c.account_id for c in customers]},
        ).all()
    assert sorted(b.ledger_balance for b in balances) == [1_000_00 - 20_00] * 3
    assert tiny_stack.probe.current == 0
