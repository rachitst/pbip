"""TASK-095 (login failures, new device or network) and TASK-034 (Redis velocity) on real transfers.

Each rule is shown to fire AND not fire, with its reason persisted (source RULE) and its inputs in
the ``fraud_events`` snapshot. The velocity rule is shown to work from Redis and, with Redis down,
from PostgreSQL.
"""

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fakeredis import FakeRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.redis import get_redis
from pbip_api.modules.auth.dependencies import get_request_meta
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.fraud.models import FraudEvent
from pbip_test_support import Actor, age_payee
from redis import Redis
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@pytest.fixture
def pair(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, dict[str, Any]]:
    """Alice (funded) with a 48h-old verified payee, so no other rule fires."""
    alice, bob = make_customer(funds=10_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    return alice, payee


@pytest.fixture
def client_ip(app: FastAPI) -> Iterator[Callable[[str], None]]:
    def use(ip: str) -> None:
        app.dependency_overrides[get_request_meta] = lambda: RequestMeta(ip=ip)

    yield use
    app.dependency_overrides.pop(get_request_meta, None)


def send(client: TestClient, alice: Actor, payee: dict[str, Any], device: str | None = None) -> Any:
    headers = {**alice.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"}
    if device is not None:
        headers["X-Device-ID"] = device
    response = client.post(
        "/api/v1/transfers",
        headers=headers,
        json={"from_account_id": str(alice.account_id), "amount_minor": 10_000,
              "payee_id": payee["id"], "reference": "rent"},
    )  # fmt: skip
    assert response.status_code in (201, 202, 403), response.text
    return response.json()


def event(db: Session, body: Any) -> FraudEvent:
    db.expire_all()
    return db.execute(
        select(FraudEvent).where(FraudEvent.transaction_id == uuid.UUID(body["id"]))
    ).scalar_one()


def codes(e: FraudEvent) -> set[str]:
    return {r["code"] for r in e.reasons}


def fail_logins(client: TestClient, actor: Actor, times: int) -> None:
    for _ in range(times):
        r = client.post("/api/v1/auth/login", json={"email": actor.email, "password": "wrong-pw-1"})
        assert r.status_code == 401


@pytest.mark.parametrize(("failures", "fires"), [(2, False), (3, True)])
def test_recent_login_failures_rule(
    client: TestClient, pair: tuple[Actor, dict[str, Any]], db: Session, failures: int, fires: bool
) -> None:
    alice, payee = pair
    fail_logins(client, alice, failures)
    e = event(db, send(client, alice, payee))
    assert ("RECENT_LOGIN_FAILURES" in codes(e)) is fires
    assert e.features["login_failures_24h"] == failures
    if fires:
        reason = next(r for r in e.reasons if r["code"] == "RECENT_LOGIN_FAILURES")
        assert reason["source"] == "RULE" and reason["message"].startswith("3 failed sign-ins")
        assert e.risk_score == 20 and e.decision == "ALLOW"  # a signal alone is below STEP_UP


def test_new_device_fires_only_against_an_existing_device_history(
    client: TestClient, pair: tuple[Actor, dict[str, Any]], db: Session
) -> None:
    alice, payee = pair
    first = event(db, send(client, alice, payee, device="browser-A"))
    assert "NEW_DEVICE_OR_NETWORK" not in codes(first)  # no history yet: nothing to differ from
    assert first.features["device_id"] and first.features["new_device"] is False
    assert "browser-A" not in str(first.features)  # only the derived id is stored
    same = event(db, send(client, alice, payee, device="browser-A"))
    assert "NEW_DEVICE_OR_NETWORK" not in codes(same)
    other = event(db, send(client, alice, payee, device="browser-B"))
    assert "NEW_DEVICE_OR_NETWORK" in codes(other) and other.features["new_device"] is True
    reason = next(r for r in other.reasons if r["code"] == "NEW_DEVICE_OR_NETWORK")
    assert reason["source"] == "RULE" and "a device" in reason["message"]
    unknown = event(db, send(client, alice, payee))  # no X-Device-ID: never adds risk
    assert "NEW_DEVICE_OR_NETWORK" not in codes(unknown)


def test_new_network_fires_and_the_full_ip_is_never_stored(
    client: TestClient,
    pair: tuple[Actor, dict[str, Any]],
    client_ip: Callable[[str], None],
    db: Session,
) -> None:
    alice, payee = pair
    client_ip("203.0.113.7")
    home = event(db, send(client, alice, payee))
    assert home.features["ip_network"] == "203.0.113.0/24" and "NEW_DEVICE_OR_NETWORK" not in codes(
        home
    )
    client_ip("203.0.113.99")  # same /24: same network
    assert "NEW_DEVICE_OR_NETWORK" not in codes(event(db, send(client, alice, payee)))
    client_ip("198.51.100.9")
    away = event(db, send(client, alice, payee))
    assert "NEW_DEVICE_OR_NETWORK" in codes(away) and away.features["new_network"] is True
    assert "198.51.100.9" not in str(away.features) and "198.51.100.0/24" in str(away.reasons)


def test_takeover_pattern_together_reaches_step_up(
    client: TestClient, pair: tuple[Actor, dict[str, Any]], db: Session
) -> None:
    alice, payee = pair
    send(client, alice, payee, device="usual-phone")
    fail_logins(client, alice, 3)
    body = send(client, alice, payee, device="attacker-laptop")
    e = event(db, body)
    assert {"RECENT_LOGIN_FAILURES", "NEW_DEVICE_OR_NETWORK"} <= codes(e)
    assert e.risk_score == 35 and e.decision == "STEP_UP"
    assert body["status"] == "PENDING_REVIEW"  # no MFA enrolled: step-up becomes review


def test_registered_devices_count_as_seen(
    client: TestClient, pair: tuple[Actor, dict[str, Any]], db: Session
) -> None:
    alice, payee = pair
    r = client.post(
        "/api/v1/customers/devices",
        headers={**alice.headers, "X-Device-ID": "registered-laptop"},
        json={"label": "Laptop"},
    )
    assert r.status_code == 201
    send(client, alice, payee, device="registered-laptop")
    assert "NEW_DEVICE_OR_NETWORK" not in codes(
        event(db, send(client, alice, payee, device="registered-laptop"))
    )
    assert "NEW_DEVICE_OR_NETWORK" in codes(event(db, send(client, alice, payee, device="unknown")))


def test_velocity_and_volume_come_from_redis_after_each_commit(
    client: TestClient, pair: tuple[Actor, dict[str, Any]], fake_redis: FakeRedis, db: Session
) -> None:
    alice, payee = pair
    bodies = [send(client, alice, payee) for _ in range(4)]
    assert int(fake_redis.zcard(f"fraud:vel:tx:{alice.account_id}")) == 4  # recorded post-commit
    last = event(db, bodies[-1])
    assert last.features["transfers_last_10m"] == 3 and last.features["volume_1h_minor"] == 30_000
    assert "VELOCITY_BURST" in codes(last)


def test_velocity_falls_back_to_postgresql_when_redis_is_down(
    client: TestClient, app: FastAPI, pair: tuple[Actor, dict[str, Any]], db: Session
) -> None:
    alice, payee = pair
    down = Redis(host="127.0.0.1", port=9, socket_connect_timeout=0.2, decode_responses=True)
    app.dependency_overrides[get_redis] = lambda: down
    bodies = [send(client, alice, payee) for _ in range(4)]  # transfers still work
    last = event(db, bodies[-1])
    assert last.features["transfers_last_10m"] == 3 and "VELOCITY_BURST" in codes(last)
    assert last.features["volume_1h_minor"] == 30_000


def test_failed_logins_are_counted_per_ip_in_redis(
    client: TestClient, pair: tuple[Actor, dict[str, Any]], fake_redis: FakeRedis
) -> None:
    alice, _payee = pair
    fail_logins(client, alice, 2)
    assert int(fake_redis.zcard("fraud:vel:loginfail:testclient")) == 2
    assert int(fake_redis.ttl("fraud:vel:loginfail:testclient")) > 0  # expires by itself
