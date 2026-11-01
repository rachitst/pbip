"""TASK-035: fraud telemetry for analysts: content, staff-only with MFA, 60-second Redis cache."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fakeredis import FakeRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.redis import get_redis
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.fraud.dependencies import get_fraud_scorer
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import Actor, age_payee
from redis import Redis
from sqlalchemy import Engine

pytestmark = pytest.mark.integration


def send(client: TestClient, alice: Actor, payee: dict[str, Any], amount: int) -> None:
    r = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={"from_account_id": str(alice.account_id), "amount_minor": amount,
              "payee_id": payee["id"], "reference": "rent"},
    )  # fmt: skip
    assert r.status_code in (201, 202, 403), r.text


@pytest.fixture
def traffic(
    app: FastAPI,
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> dict[str, Any]:
    """Two ordinary transfers, one rules-flagged (new payee + large), one forced BLOCK."""
    alice, bob = make_customer(funds=10_000_000), make_customer()
    old = make_verified_payee(alice, bob)
    age_payee(migrated_engine, old["id"], 48)
    send(client, alice, old, 10_000)
    send(client, alice, old, 12_000)
    carol = make_customer()
    fresh = make_verified_payee(alice, carol)  # minutes old: NEW_PAYEE_LARGE_AMOUNT -> review
    send(client, alice, fresh, 3_000_000)
    app.dependency_overrides[get_fraud_scorer] = lambda: FixedDecisionScorer(FraudDecision.BLOCK)
    send(client, alice, old, 5_000)
    app.dependency_overrides.pop(get_fraud_scorer)
    return {"fresh": fresh}


def test_stats_report_decisions_rules_and_masked_payees(
    client: TestClient, make_staff: Callable[..., Actor], traffic: dict[str, Any]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    body = client.get("/api/v1/fraud/stats?days=7", headers=analyst.headers).json()
    assert body["window_days"] == 7 and body["scored"] == 4
    assert body["totals"] == {"ALLOW": 2, "STEP_UP": 1, "REVIEW": 0, "BLOCK": 1}
    assert body["block_rate"] == 0.25 and body["hold_rate"] == 0.0
    assert sum(d["ALLOW"] + d["STEP_UP"] + d["REVIEW"] + d["BLOCK"] for d in body["by_day"]) == 4
    codes = {r["code"]: r for r in body["top_rules"]}
    assert codes["NEW_PAYEE_LARGE_AMOUNT"]["count"] == 1
    assert codes["NEW_PAYEE_LARGE_AMOUNT"]["source"] == "RULE"
    # Only the BLOCKed transfer's payee: the STEP_UP one (risk 45) is below the REVIEW line.
    assert [(p["flagged_transfers"], p["customers"]) for p in body["risky_payees"]] == [(1, 1)]
    assert body["risky_payees"][0]["account"].startswith("\u2022")  # masked
    assert body["model_fallbacks"] == 0 and body["latency_p95_ms"] is not None


def test_risky_payees_are_masked_and_counted_across_customers(
    app: FastAPI,
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    mule = make_customer()
    app.dependency_overrides[get_fraud_scorer] = lambda: FixedDecisionScorer(FraudDecision.REVIEW)
    for _ in range(2):
        sender = make_customer(funds=1_000_000)
        payee = make_verified_payee(sender, mule)
        age_payee(migrated_engine, payee["id"], 48)
        send(client, sender, payee, 10_000)
    app.dependency_overrides.pop(get_fraud_scorer)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    payees = client.get("/api/v1/fraud/stats", headers=analyst.headers).json()["risky_payees"]
    assert len(payees) == 1 and payees[0]["customers"] == 2 and payees[0]["flagged_transfers"] == 2
    assert mule.account_number is not None
    assert payees[0]["account"].endswith(mule.account_number[-4:])
    assert mule.account_number not in str(payees)  # never the full number


def test_stats_are_staff_only_and_need_mfa(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor]
) -> None:
    assert client.get("/api/v1/fraud/stats").status_code == 401
    assert client.get("/api/v1/fraud/stats", headers=make_customer().headers).status_code == 403
    no_mfa = make_staff(RoleName.FRAUD_ANALYST, mfa=False)
    denied = client.get("/api/v1/fraud/stats", headers=no_mfa.headers)
    assert denied.status_code == 403 and denied.json()["code"] == "mfa_required"
    assert (
        client.get("/api/v1/fraud/stats", headers=make_staff(RoleName.ADMIN).headers).status_code
        == 200
    )
    for bad in ("0", "91", "x"):
        r = client.get(
            f"/api/v1/fraud/stats?days={bad}", headers=make_staff(RoleName.ADMIN).headers
        )
        assert r.status_code == 422


def test_stats_are_cached_for_60_seconds_and_survive_redis_down(
    app: FastAPI,
    client: TestClient,
    make_staff: Callable[..., Actor],
    fake_redis: FakeRedis,
    traffic: dict[str, Any],
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    first = client.get("/api/v1/fraud/stats", headers=analyst.headers).json()
    assert 0 < int(fake_redis.ttl("fraud:stats:v1:7")) <= 60
    alice = make_customer(funds=1_000_000)
    send(client, alice, make_verified_payee(alice, make_customer()), 1_000)  # one more event
    assert client.get("/api/v1/fraud/stats", headers=analyst.headers).json() == first  # cached
    down = Redis(host="127.0.0.1", port=9, socket_connect_timeout=0.2, decode_responses=True)
    app.dependency_overrides[get_redis] = lambda: down
    fresh = client.get("/api/v1/fraud/stats", headers=analyst.headers)
    assert fresh.status_code == 200 and fresh.json()["scored"] == first["scored"] + 1
