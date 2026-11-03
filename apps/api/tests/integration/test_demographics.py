"""TASK-107 / ADR-0015: consented date of birth and the age band, against PostgreSQL."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.customers.models import Customer
from pbip_test_support import Actor, FakeClock
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]

ME = "/api/v1/customers/me/demographics"
DOB = "/api/v1/customers/me/date-of-birth"
CONSENT = "/api/v1/customers/me/recommendations-consent"
# 12:00 IST on 2 November 2026: the bank's "today" for age checks.
NOW = datetime(2026, 11, 2, 6, 30, tzinfo=UTC)
SENIOR_DOB = "1958-04-17"  # 68 -> 60+; a distinctive value to search responses and logs for


@pytest.fixture
def at_now(clock: FakeClock) -> FakeClock:
    clock.now = NOW.timestamp()
    return clock


def _actions(db: Session, actor: Actor) -> list[AuditLog]:
    return list(
        db.execute(
            select(AuditLog)
            .where(AuditLog.actor_user_id == actor.user_id, AuditLog.resource_type == "customer")
            .order_by(AuditLog.seq)
        ).scalars()
    )


def test_new_customers_have_nothing_inferred(
    client: TestClient, make_customer: Callable[..., Actor], at_now: FakeClock, db: Session
) -> None:
    alice = make_customer()
    assert client.get(ME, headers=alice.headers).json() == {
        "date_of_birth": None,
        "age_band": "unknown",
        "recommendations_consent": False,
        "consent_granted_at": None,
        "consent_policy_version": None,
        "current_policy_version": "ADR-0015-v1",
    }
    row = db.get(Customer, alice.customer_id)
    assert row is not None and row.date_of_birth is None and row.recommendations_consent_at is None


def test_set_and_delete_date_of_birth_are_audited_with_the_policy_and_without_the_date(
    client: TestClient, make_customer: Callable[..., Actor], at_now: FakeClock, db: Session
) -> None:
    alice = make_customer()
    resp = client.put(DOB, headers=alice.headers, json={"date_of_birth": SENIOR_DOB})
    assert resp.status_code == 200, resp.text
    assert resp.json()["date_of_birth"] == SENIOR_DOB and resp.json()["age_band"] == "60+"
    assert resp.json()["recommendations_consent"] is False  # setting a date is not consent
    # the same date again is not a change: no second audit row
    client.put(DOB, headers=alice.headers, json={"date_of_birth": SENIOR_DOB})
    corrected = client.put(DOB, headers=alice.headers, json={"date_of_birth": "1990-06-01"})
    assert corrected.json()["age_band"] == "36-50"

    assert client.delete(DOB, headers=alice.headers).status_code == 204
    assert client.delete(DOB, headers=alice.headers).status_code == 204  # already gone: no-op
    after = client.get(ME, headers=alice.headers).json()
    assert after["date_of_birth"] is None and after["age_band"] == "unknown"

    rows = _actions(db, alice)
    assert [r.action for r in rows] == [
        "DATE_OF_BIRTH_SET",
        "DATE_OF_BIRTH_SET",
        "DATE_OF_BIRTH_DELETED",
    ]
    assert [r.details.get("replaced_existing") for r in rows[:2]] == [False, True]
    assert all(r.details["policy_version"] == "ADR-0015-v1" for r in rows)
    assert all(r.resource_id == str(alice.customer_id) for r in rows)
    for row in rows:  # the audit trail is staff-readable, so it never holds the date
        assert "1958" not in str(row.details) and "1990" not in str(row.details)


@pytest.mark.parametrize(
    "body",
    [
        {"date_of_birth": "2008-11-03"},  # 17, 18 tomorrow
        {"date_of_birth": "1905-11-01"},  # 121
        {"date_of_birth": "2027-01-01"},  # future
    ],
)
def test_ages_outside_18_to_120_on_the_bank_clock_are_refused(
    client: TestClient,
    make_customer: Callable[..., Actor],
    at_now: FakeClock,
    db: Session,
    body: dict[str, Any],
) -> None:
    alice = make_customer()
    resp = client.put(DOB, headers=alice.headers, json=body)
    assert resp.status_code == 422 and resp.json()["code"] == "unprocessable"
    assert body["date_of_birth"] not in resp.text  # not echoed
    assert client.get(ME, headers=alice.headers).json()["date_of_birth"] is None
    assert _actions(db, alice) == []


def test_age_limits_follow_the_injected_clock_at_the_boundary(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock
) -> None:
    alice = make_customer()
    clock.now = datetime(2026, 11, 2, 18, 29, tzinfo=UTC).timestamp()  # 23:59 IST, 2 Nov
    refused = client.put(DOB, headers=alice.headers, json={"date_of_birth": "2008-11-03"})
    assert refused.status_code == 422
    clock.now = datetime(2026, 11, 2, 18, 30, tzinfo=UTC).timestamp()  # 00:00 IST, 3 Nov
    accepted = client.put(DOB, headers=alice.headers, json={"date_of_birth": "2008-11-03"})
    assert accepted.status_code == 200 and accepted.json()["age_band"] == "18-25"
    oldest = client.put(DOB, headers=alice.headers, json={"date_of_birth": "1906-11-03"})
    assert oldest.status_code == 200  # exactly 120 on 3 Nov IST


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"date_of_birth": "17/04/1958"},
        {"date_of_birth": "1958-02-30"},
        {"date_of_birth": -369_878_400},  # epoch seconds are not a date
        {"date_of_birth": SENIOR_DOB, "customer_id": "00000000-0000-0000-0000-000000000001"},
    ],
)
def test_malformed_dates_are_rejected(
    client: TestClient, make_customer: Callable[..., Actor], at_now: FakeClock, body: Any
) -> None:
    alice = make_customer()
    resp = client.put(DOB, headers=alice.headers, json=body)
    assert resp.status_code == 422 and resp.json()["code"] == "validation_error"
    assert "17/04/1958" not in resp.text


@pytest.mark.parametrize("body", [{}, {"granted": "yes"}, {"granted": 1}, {"granted": None}])
def test_consent_must_be_an_explicit_boolean(
    client: TestClient, make_customer: Callable[..., Actor], body: dict[str, Any]
) -> None:
    alice = make_customer()
    assert client.put(CONSENT, headers=alice.headers, json=body).status_code == 422
    assert client.get(ME, headers=alice.headers).json()["recommendations_consent"] is False


def test_consent_grant_and_withdrawal_are_recorded_and_audited_with_the_policy_version(
    client: TestClient, make_customer: Callable[..., Actor], at_now: FakeClock, db: Session
) -> None:
    alice = make_customer()
    granted = client.put(CONSENT, headers=alice.headers, json={"granted": True})
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert body["recommendations_consent"] is True
    assert datetime.fromisoformat(body["consent_granted_at"]) == NOW
    assert body["consent_policy_version"] == "ADR-0015-v1"
    client.put(CONSENT, headers=alice.headers, json={"granted": True})  # no change, no audit

    withdrawn = client.put(CONSENT, headers=alice.headers, json={"granted": False}).json()
    assert withdrawn["recommendations_consent"] is False
    assert withdrawn["consent_granted_at"] is None and withdrawn["consent_policy_version"] is None
    client.put(CONSENT, headers=alice.headers, json={"granted": False})  # no change, no audit

    rows = _actions(db, alice)
    assert [r.action for r in rows] == [
        "RECOMMENDATIONS_CONSENT_GRANTED",
        "RECOMMENDATIONS_CONSENT_WITHDRAWN",
    ]
    assert all(
        r.details == {"policy_version": "ADR-0015-v1", "purpose": "product_recommendations"}
        for r in rows
    )


def test_each_customer_sees_and_changes_only_their_own_data(
    client: TestClient, make_customer: Callable[..., Actor], at_now: FakeClock, db: Session
) -> None:
    alice, bob = make_customer(), make_customer()
    client.put(DOB, headers=alice.headers, json={"date_of_birth": SENIOR_DOB})
    client.put(CONSENT, headers=alice.headers, json={"granted": True})

    bob_view = client.get(f"{ME}?customer_id={alice.customer_id}", headers=bob.headers)
    assert bob_view.json()["date_of_birth"] is None and SENIOR_DOB not in bob_view.text
    assert (
        client.delete(f"{DOB}?customer_id={alice.customer_id}", headers=bob.headers).status_code
        == 204
    )
    client.put(CONSENT, headers=bob.headers, json={"granted": False})

    alice_row = db.get(Customer, alice.customer_id)
    assert alice_row is not None
    assert alice_row.date_of_birth == date(1958, 4, 17)
    assert alice_row.recommendations_consent_at is not None


def test_staff_and_anonymous_callers_cannot_use_the_routes(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("get", ME, None),
        ("put", DOB, {"date_of_birth": SENIOR_DOB}),
        ("delete", DOB, None),
        ("put", CONSENT, {"granted": True}),
    ]
    for method, path, body in calls:
        kwargs: dict[str, Any] = {} if body is None else {"json": body}
        assert client.request(method, path, **kwargs).status_code == 401, path
        assert client.request(method, path, headers=rm.headers, **kwargs).status_code == 403, path


def test_date_of_birth_never_reaches_the_logs(
    client: TestClient,
    make_customer: Callable[..., Actor],
    at_now: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    alice = make_customer()
    capsys.readouterr()
    client.put(DOB, headers=alice.headers, json={"date_of_birth": SENIOR_DOB})
    client.get(ME, headers=alice.headers)
    client.get("/api/v1/customers/me", headers=alice.headers)
    client.put(DOB, headers=alice.headers, json={"date_of_birth": "2015-01-01"})  # refused
    client.get("/api/v1/recommendations", headers=alice.headers)
    captured = capsys.readouterr()
    logs = captured.out + captured.err
    assert "/api/v1/customers/me/date-of-birth" in logs  # the requests were logged...
    for leaked in (SENIOR_DOB, "2015-01-01"):  # ...without the date
        assert leaked not in logs


def test_database_refuses_consent_without_its_policy_version(
    make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
        connection.execute(
            text("UPDATE customers SET recommendations_consent_at = now() WHERE id = :id"),
            {"id": alice.customer_id},
        )


def _senior_with_consent(client: TestClient, actor: Actor) -> None:
    assert (
        client.put(DOB, headers=actor.headers, json={"date_of_birth": SENIOR_DOB}).status_code
        == 200
    )
    assert client.put(CONSENT, headers=actor.headers, json={"granted": True}).status_code == 200


def test_staff_responses_never_contain_the_date_of_birth(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    at_now: FakeClock,
    assign_rm: Callable[..., None],
) -> None:
    """RM insights, the fraud alert view and the audit trail show the band at most (ADR-0015)."""
    alice, bob = make_customer(funds=10_000_000), make_customer()
    _senior_with_consent(client, alice)
    payee = make_verified_payee(alice, bob)
    held = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": str(uuid4())},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 3_000_000,
            "payee_id": payee["id"],
            "reference": "rent",
        },
    )
    assert held.status_code == 202, held.text  # new payee + large amount -> fraud alert

    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    assign_rm(rm, alice)  # an RM sees only assigned customers (ADR-0032)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    alerts = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()
    assert alerts
    staff_views = [
        client.get("/api/v1/insights/customers", headers=rm.headers),
        client.get(f"/api/v1/insights/customers/{alice.customer_id}", headers=rm.headers),
        client.get(f"/api/v1/fraud/alerts/{alerts[0]['id']}", headers=analyst.headers),
        client.get("/api/v1/compliance/audit-logs?limit=200", headers=officer.headers),
    ]
    for view in staff_views:
        assert view.status_code == 200, view.text
        assert SENIOR_DOB not in view.text and "date_of_birth" not in view.text
    alert_customer = staff_views[2].json()["customer"]
    assert "date_of_birth" not in alert_customer and "age_band" not in alert_customer
    actions = {row["action"] for row in staff_views[3].json()}
    assert {"DATE_OF_BIRTH_SET", "RECOMMENDATIONS_CONSENT_GRANTED"} <= actions


def test_assistant_tool_output_never_contains_the_date_of_birth(
    client: TestClient, make_customer: Callable[..., Actor], at_now: FakeClock
) -> None:
    alice = make_customer(funds=1_00_000_00)
    _senior_with_consent(client, alice)
    questions = (
        "What's my balance?",
        "show my last 3 transactions",
        "how much did I spend this month",
        "forecast my balance for next week",
        "can I afford a 2 lakh personal loan over 2 years",
        "what is my date of birth?",
        "how old am I",
    )
    for question in questions:
        resp = client.post(
            "/api/v1/assistant/chat", headers=alice.headers, json={"message": question}
        )
        assert resp.status_code == 200, resp.text
        assert SENIOR_DOB not in resp.text and "17 April 1958" not in resp.text
    tools_used = {
        m.get("tool")
        for m in client.get("/api/v1/assistant/messages", headers=alice.headers).json()["items"]
    }
    assert len(tools_used - {None}) == 5  # every tool really ran
    history = client.get("/api/v1/assistant/messages", headers=alice.headers)
    assert SENIOR_DOB not in history.text
