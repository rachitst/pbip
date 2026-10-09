"""Test doubles, app builders and integration helpers (imported via pytest `pythonpath`)."""

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pyotp
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import get_engine
from pbip_api.core.redis import get_redis
from pbip_api.main import create_app
from sqlalchemy import Engine, text

PASSWORD = "Correct-Horse-9!"


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
