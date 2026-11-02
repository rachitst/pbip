"""TASK-099 (UC-13): the security monitoring panels for analysts, from real sign-ins and transfers.

Every event here goes through the API (failed logins, lockouts, flagged transfers, an analyst's
decision), so the panels are checked against what the platform really records.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from fakeredis import FakeRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.redis import get_redis
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.fraud.dependencies import get_fraud_scorer
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import PASSWORD, Actor, FakeClock, age_payee
from redis import Redis
from sqlalchemy import Engine

pytestmark = pytest.mark.integration

URL = "/api/v1/fraud/monitoring"


@pytest.fixture(autouse=True)
def roomy_limits(settings: Settings) -> None:
    """These tests sign in (and fail to) more often than the per-IP limits allow in a minute."""
    settings.rate_limit_login_per_minute = 1_000
    settings.rate_limit_register_per_hour = 1_000


def send(
    app: FastAPI,
    client: TestClient,
    sender: Actor,
    payee: dict[str, Any],
    decision: FraudDecision,
    amount: int = 10_000,
) -> str:
    app.dependency_overrides[get_fraud_scorer] = lambda: FixedDecisionScorer(decision)
    try:
        r = client.post(
            "/api/v1/transfers",
            headers={**sender.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
            json={"from_account_id": str(sender.account_id), "amount_minor": amount,
                  "payee_id": payee["id"], "reference": "rent"},
        )  # fmt: skip
    finally:
        app.dependency_overrides.pop(get_fraud_scorer)
    assert r.status_code in (201, 202, 403), r.text
    return str(r.json().get("id", ""))


def fail_login(client: TestClient, email: str, times: int) -> None:
    for _ in range(times):
        wrong = "wrong-" + PASSWORD
        r = client.post("/api/v1/auth/login", json={"email": email, "password": wrong})
        assert r.status_code == 401, r.text


@pytest.fixture
def payer(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> Callable[[], tuple[Actor, dict[str, Any]]]:
    """A funded customer with an established payee (so only the forced decision matters)."""

    def build() -> tuple[Actor, dict[str, Any]]:
        sender = make_customer(funds=10_000_000)
        payee = make_verified_payee(sender, make_customer())
        age_payee(migrated_engine, payee["id"], 48)
        return sender, payee

    return build


def test_empty_state_is_all_zeros_and_empty_lists(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    body = client.get(URL, headers=make_staff(RoleName.FRAUD_ANALYST).headers).json()
    assert body["high_risk_customers"] == [] and body["high_risk_window_days"] == 30
    assert body["open_alerts"] == {
        "total": 0,
        "by_severity": {"MEDIUM": 0, "HIGH": 0},
        "oldest_open_at": None,
        "oldest_open_age_seconds": None,
        "oldest": [],
    }
    logins = body["login_anomalies"]
    assert logins["bursts"] == [] and logins["recent_lockouts"] == []
    assert logins["window_hours"] == 24 and logins["burst_threshold"] == 3
    assert set(logins["counts"].values()) == {0} and logins["unknown_account_failures"] == 0
    assert logins["counts"].keys() == {
        "LOGIN_FAILURE", "ACCOUNT_LOCKED", "LOGIN_BLOCKED_LOCKED", "MFA_FAILURE", "STEP_UP_FAILURE"
    }  # fmt: skip
    assert body["fraud_events"] == {"last_24h": {"REVIEW": 0, "BLOCK": 0}, "latest": []}


def test_high_risk_customers_open_alerts_and_flagged_events(
    app: FastAPI,
    client: TestClient,
    make_staff: Callable[..., Actor],
    payer: Callable[[], tuple[Actor, dict[str, Any]]],
) -> None:
    alice, alice_payee = payer()
    bob, bob_payee = payer()
    carol, carol_payee = payer()
    send(app, client, alice, alice_payee, FraudDecision.REVIEW)
    send(app, client, alice, alice_payee, FraudDecision.REVIEW)
    send(app, client, alice, alice_payee, FraudDecision.BLOCK)
    send(app, client, bob, bob_payee, FraudDecision.REVIEW)
    send(app, client, carol, carol_payee, FraudDecision.ALLOW)  # not flagged: never listed
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    queue = client.get("/api/v1/fraud/alerts?status=OPEN", headers=analyst.headers).json()
    assert len(queue) == 4
    first_review = next(a for a in reversed(queue) if a["decision"] == "REVIEW")  # alice's oldest
    decided = client.post(
        f"/api/v1/fraud/alerts/{first_review['id']}/decision",
        headers=analyst.headers,
        json={"action": "REJECT", "note": "confirmed with the customer"},
    )
    assert decided.status_code == 204, decided.text

    body = client.get(URL, headers=analyst.headers).json()
    top, second = body["high_risk_customers"]
    assert top["customer_id"] == str(alice.customer_id)
    assert (top["flagged_events"], top["blocked"], top["max_risk"], top["open_alerts"]) == (
        3, 1, 95, 2
    )  # fmt: skip
    open_ids = {a["id"] for a in queue} - {first_review["id"]}
    assert top["alert_id"] in open_ids  # drill-down opens an alert still waiting
    assert second["customer_id"] == str(bob.customer_id) and second["flagged_events"] == 1

    alerts = body["open_alerts"]
    assert alerts["total"] == 3 and alerts["by_severity"] == {"MEDIUM": 2, "HIGH": 1}
    assert [a["alert_id"] for a in alerts["oldest"]] == [
        a["id"] for a in reversed(queue) if a["id"] in open_ids
    ]  # oldest first, the decided alert gone
    assert alerts["oldest_open_at"] == alerts["oldest"][0]["created_at"]

    events = body["fraud_events"]
    assert events["last_24h"] == {"REVIEW": 3, "BLOCK": 1}
    latest = events["latest"]
    assert [e["decision"] for e in latest] == ["REVIEW", "BLOCK", "REVIEW", "REVIEW"]
    assert {e["alert_id"] for e in latest} == {a["id"] for a in queue}
    rejected = next(e for e in latest if e["alert_id"] == first_review["id"])
    assert rejected["alert_status"] == "CONFIRMED_FRAUD"
    assert latest[0]["customer_id"] == str(bob.customer_id)
    assert latest[0]["reason_codes"] == ["TEST_FIXED_DECISION"]
    assert latest[0]["amount_minor"] == 10_000 and latest[0]["currency"] == "INR"


def test_an_escalated_step_up_counts_as_flagged(
    app: FastAPI,
    client: TestClient,
    make_staff: Callable[..., Actor],
    payer: Callable[[], tuple[Actor, dict[str, Any]]],
) -> None:
    """A STEP_UP the customer cannot complete (no MFA) is escalated: the alert is on that event."""
    sender, payee = payer()
    send(app, client, sender, payee, FraudDecision.STEP_UP)
    body = client.get(URL, headers=make_staff(RoleName.FRAUD_ANALYST).headers).json()
    [event] = body["fraud_events"]["latest"]
    assert event["decision"] == "STEP_UP" and event["alert_id"] is not None
    assert body["high_risk_customers"][0]["customer_id"] == str(sender.customer_id)
    assert body["fraud_events"]["last_24h"] == {"REVIEW": 0, "BLOCK": 0}  # decisions, not alerts


def test_oldest_open_age_follows_the_injected_clock(
    app: FastAPI,
    client: TestClient,
    clock: FakeClock,
    make_staff: Callable[..., Actor],
    payer: Callable[[], tuple[Actor, dict[str, Any]]],
) -> None:
    sender, payee = payer()
    send(app, client, sender, payee, FraudDecision.REVIEW)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    clock.advance(3 * 3600)
    alerts = client.get(URL, headers=analyst.headers).json()["open_alerts"]
    raised = datetime.fromisoformat(alerts["oldest_open_at"]).timestamp()
    assert alerts["oldest_open_age_seconds"] == int(clock.now - raised)
    assert alerts["oldest_open_age_seconds"] >= 3 * 3600


def test_login_anomalies_find_bursts_and_lockouts_without_ips_or_emails(
    client: TestClient,
    clock: FakeClock,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    burst, quiet, locked = make_customer(), make_customer(), make_customer()
    fail_login(client, burst.email, 3)
    fail_login(client, quiet.email, 2)  # under the threshold
    fail_login(client, locked.email, 5)  # the fifth failure locks the account
    fail_login(client, locked.email, 1)  # while locked: LOGIN_BLOCKED_LOCKED, not a failure
    fail_login(client, "nobody@example.com", 2)
    body = client.get(URL, headers=make_staff(RoleName.FRAUD_ANALYST).headers).json()
    logins = body["login_anomalies"]
    assert [(b["user_id"], b["failures"]) for b in logins["bursts"]] == [
        (str(locked.user_id), 5),
        (str(burst.user_id), 3),
    ]
    assert [lock["user_id"] for lock in logins["recent_lockouts"]] == [str(locked.user_id)]
    assert logins["counts"]["LOGIN_FAILURE"] == 3 + 2 + 5 + 2
    assert logins["counts"]["ACCOUNT_LOCKED"] == 1
    assert logins["counts"]["LOGIN_BLOCKED_LOCKED"] == 1
    assert logins["unknown_account_failures"] == 2
    text = str(body)
    for actor in (burst, quiet, locked):
        assert actor.email not in text
    assert "nobody@example.com" not in text and "testclient" not in text  # no IPs either


def test_login_window_is_24_hours_of_the_injected_clock(
    client: TestClient,
    clock: FakeClock,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    locked = make_customer()
    fail_login(client, locked.email, 5)
    clock.advance(25 * 3600)
    logins = client.get(URL, headers=make_staff(RoleName.FRAUD_ANALYST).headers).json()[
        "login_anomalies"
    ]
    assert logins["bursts"] == [] and logins["counts"]["LOGIN_FAILURE"] == 0
    # The lockout history is not windowed: the newest lockouts stay visible.
    assert [lock["user_id"] for lock in logins["recent_lockouts"]] == [str(locked.user_id)]


def test_no_customer_pii_in_any_panel(
    app: FastAPI,
    client: TestClient,
    make_staff: Callable[..., Actor],
    payer: Callable[[], tuple[Actor, dict[str, Any]]],
) -> None:
    sender, payee = payer()
    send(app, client, sender, payee, FraudDecision.BLOCK)
    fail_login(client, sender.email, 3)
    text = client.get(URL, headers=make_staff(RoleName.FRAUD_ANALYST).headers).text
    assert str(sender.customer_id) in text  # ids are there ...
    assert sender.email not in text and "Test Customer" not in text  # ... names/emails are not
    assert sender.account_number is not None and sender.account_number not in text
    assert payee["name"] not in text and "rent" not in text  # nor the payee or the reference


def test_monitoring_needs_a_staff_session_verified_with_mfa(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor]
) -> None:
    assert client.get(URL).status_code == 401
    customer = client.get(URL, headers=make_customer().headers)
    assert customer.status_code == 403
    assert customer.headers["content-type"].startswith("application/problem+json")
    no_mfa = client.get(URL, headers=make_staff(RoleName.FRAUD_ANALYST, mfa=False).headers)
    assert no_mfa.status_code == 403 and no_mfa.json()["code"] == "mfa_required"


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (RoleName.FRAUD_ANALYST, 200),
        (RoleName.ADMIN, 200),
        (RoleName.COMPLIANCE_OFFICER, 403),  # role separation: fraud data is the analysts'
        (RoleName.RELATIONSHIP_MANAGER, 403),
    ],
)
def test_only_fraud_analysts_and_admins_see_the_panels(
    client: TestClient, make_staff: Callable[..., Actor], role: RoleName, expected: int
) -> None:
    assert client.get(URL, headers=make_staff(role).headers).status_code == expected


def test_monitoring_is_cached_for_60_seconds_and_survives_redis_down(
    app: FastAPI,
    client: TestClient,
    fake_redis: FakeRedis,
    make_staff: Callable[..., Actor],
    payer: Callable[[], tuple[Actor, dict[str, Any]]],
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    sender, payee = payer()
    first = client.get(URL, headers=analyst.headers).json()
    assert 0 < int(fake_redis.ttl("fraud:monitoring:v1")) <= 60
    send(app, client, sender, payee, FraudDecision.REVIEW)
    assert client.get(URL, headers=analyst.headers).json() == first  # still the cached panels
    down = Redis(host="127.0.0.1", port=9, socket_connect_timeout=0.2, decode_responses=True)
    app.dependency_overrides[get_redis] = lambda: down
    fresh = client.get(URL, headers=analyst.headers)
    assert fresh.status_code == 200 and len(fresh.json()["fraud_events"]["latest"]) == 1
