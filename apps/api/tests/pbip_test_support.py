"""Test doubles, app builders and integration helpers (imported via pytest `pythonpath`)."""

import importlib.util
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import fakeredis
import pyotp
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import get_engine
from pbip_api.core.redis import get_redis
from pbip_api.main import create_app
from pbip_api.modules.wealth import deposit_math
from pbip_api.modules.wealth.schemas import WealthProductOut
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import Engine, event, text

PASSWORD = "Correct-Horse-9!"


def generated_password() -> str:
    """A throwaway password that always passes the policy.

    Random hex is split into groups of four because a raw uuid can hold a 5-character run such as
    ``23456``, which the keyboard-sequence rule rejects (about 1 in 4,000; it failed CI on PR #344).
    """
    hex_digits = uuid4().hex
    return "-".join(hex_digits[i : i + 4] for i in range(0, 32, 4)) + "-Aa1!"


class FakeConnection:
    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class FakeEngine:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    def connect(self) -> FakeConnection:
        if not self.healthy:
            raise RuntimeError("database down")
        return FakeConnection()


class FakeRedis:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    def ping(self) -> bool:
        if not self.healthy:
            raise RuntimeError("redis down")
        return True

    def get(self, _key: str) -> None:
        """Empty store: ``/metrics`` reads the stored PSI result (ADR-0038) and finds none."""
        if not self.healthy:
            raise RedisConnectionError("redis down")
        return None


def unreachable_redis() -> fakeredis.FakeRedis:
    """A Redis client whose every command raises ``redis.ConnectionError`` at once.

    It stands in for "Redis is down" without dialling a dead port: a real client pointed at
    127.0.0.1:9 waits out its connect timeout and retries, which took 14-74 s per test on Windows
    (#317). The error type is the one a real outage raises, so the fallback paths are the same.
    """
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis.FakeRedis(server=server, decode_responses=True)


@contextmanager
def count_statements(engine: Engine) -> Iterator[list[str]]:
    """Collect every SQL statement ``engine`` sends while the block runs.

    Cost checks assert on this count instead of wall-clock time (#317): a query count is the same on
    every machine and under any load, and an N+1 regression changes it, whereas a timing bound fails
    on a busy CI runner and passes a slow query on a fast laptop.
    """
    sent: list[str] = []

    def record(*args: Any) -> None:
        sent.append(str(args[2]))  # (conn, cursor, statement, parameters, context, executemany)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        event.remove(engine, "before_cursor_execute", record)


def build_app(db_ok: bool = True, redis_ok: bool = True, **overrides: Any) -> FastAPI:
    app = create_app(Settings(app_env="local", **overrides))
    app.dependency_overrides[get_engine] = lambda: FakeEngine(db_ok)
    app.dependency_overrides[get_redis] = lambda: FakeRedis(redis_ok)
    return app


class FakeClock:
    """Deterministic clock shared by the app and the test so TOTP steps are controllable."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Actor:
    """A logged-in test user."""

    email: str
    user_id: UUID
    access_token: str
    customer_id: UUID | None = None
    account_id: UUID | None = None
    account_number: str | None = None
    totp_secret: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


def totp_code(secret: str, clock: FakeClock) -> str:
    return pyotp.TOTP(secret).at(int(clock.now))


def login_json(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, Any]:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return dict(response.json())


def age_payee(engine: Engine, payee_id: str, hours: int) -> None:
    """Pretend a payee was added ``hours`` ago (to leave the 'new payee' fraud window)."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE payees SET created_at = now() - make_interval(hours => :h) WHERE id = :id"
            ),
            {"h": hours, "id": payee_id},
        )


def reset_tracing() -> None:
    """Undo tracing setup between tests.

    Test-only: the OpenTelemetry API lets a process set the global provider once, so this clears
    its private once-flag. Production code never does this (`core/tracing.py` is idempotent).
    """
    from opentelemetry import trace
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from pbip_api.core import tracing

    if SQLAlchemyInstrumentor().is_instrumented_by_opentelemetry:
        SQLAlchemyInstrumentor().uninstrument()
    if tracing._tracer_provider is not None:
        tracing._tracer_provider.shutdown()
    tracing._tracer_provider = None
    tracing._in_memory_exporter = None
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False


def post_spend_at(
    engine: Engine, account_id: UUID, amount: int, reference: str, when: datetime
) -> UUID:
    """Post a spend debit through the ledger service, then date it ``when`` (history fixtures).

    Spending baselines need months of history, but entries take ``now()`` when posted. The money
    goes through ``ledger.create_transaction``/``post_transaction`` like any payment (balances,
    balance check and seq untouched); only the timestamps of that one transaction are moved,
    with the append-only trigger lifted for this statement inside the same database transaction
    (as the audit tamper test does). Test-only: production never rewrites a ledger timestamp.
    """
    from pbip_api.modules.ledger import service as ledger
    from pbip_api.modules.ledger.models import TransactionType
    from sqlalchemy.orm import sessionmaker

    with sessionmaker(bind=engine)() as session:
        txn = ledger.create_transaction(
            session,
            type_=TransactionType.BILL_PAYMENT,
            amount=amount,
            currency="INR",
            debit_account_id=account_id,
            credit_account_id=ledger.SYSTEM_CLEARING_ID,
            reference=reference,
        )
        ledger.post_transaction(session, txn, ledger.transfer_legs(txn))
        session.commit()
        txn_id = txn.id
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE transaction_entries DISABLE TRIGGER transaction_entries_immutable")
        )
        connection.execute(
            text("UPDATE transaction_entries SET created_at = :when WHERE transaction_id = :id"),
            {"when": when, "id": txn_id},
        )
        connection.execute(
            text("UPDATE transactions SET created_at = :when, posted_at = :when WHERE id = :id"),
            {"when": when, "id": txn_id},
        )
        connection.execute(
            text("ALTER TABLE transaction_entries ENABLE TRIGGER transaction_entries_immutable")
        )
    return txn_id


def month_start(now: datetime, months_back: int) -> datetime:
    """Midnight UTC on the 1st of the calendar month ``months_back`` months before ``now``'s."""
    index = now.year * 12 + now.month - 1 - months_back
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _wealth_migration() -> ModuleType:
    path = next(_VERSIONS.glob("*_0018_wealth_catalogue_and_fixed_deposits.py"))
    spec = importlib.util.spec_from_file_location("wealth_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seeded_catalogue() -> list[WealthProductOut]:
    """The wealth catalogue exactly as migration 0018 seeds it, in the API's output shape."""
    out = []
    for row in _wealth_migration().PRODUCTS:
        rate = row["annual_rate_bps"]
        guaranteed = row["yield_basis"] == "GUARANTEED"
        out.append(
            WealthProductOut(
                id=UUID(str(row["id"])),
                code=row["code"],
                product_type=row["product_type"],
                name=row["name"],
                description=row["description"],
                risk_level=row["risk_level"],
                yield_basis=row["yield_basis"],
                annual_rate_bps=rate,
                apy_bps=deposit_math.apy_bps(rate) if guaranteed else rate,
                tenures_months=row["tenures_months"],
                min_lock_in_months=row["min_lock_in_months"],
                min_amount_minor=row["min_amount_minor"],
                max_amount_minor=row["max_amount_minor"],
                premature_closure_allowed=row["premature_penalty_bps"] is not None,
                premature_penalty_bps=row["premature_penalty_bps"],
                bookable=row["bookable"],
                notes=row["notes"],
                catalogue_version=row["catalogue_version"],
            )
        )
    return out
