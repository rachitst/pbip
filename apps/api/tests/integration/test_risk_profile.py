"""TASK-097: GET/PUT /api/v1/customers/me/risk-profile against PostgreSQL."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

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

URL = "/api/v1/customers/me/risk-profile"
ASSESSED = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)

CAUTIOUS: dict[str, str] = {
    "investment_horizon": "UNDER_1_YEAR",
    "loss_reaction": "SELL_EVERYTHING",
    "investment_goal": "PROTECT_SAVINGS",
    "investment_experience": "SOME",
    "emergency_fund": "THREE_TO_6_MONTHS",
}  # 0+0+0+1+1 = 2 -> LOW
LOW_EDGE = {**CAUTIOUS, "investment_goal": "STEADY_GROWTH"}  # 3 -> LOW
MODERATE_EDGE = {**LOW_EDGE, "loss_reaction": "WAIT_AND_HOLD"}  # 4 -> MODERATE
MODERATE_TOP = {**MODERATE_EDGE, "investment_horizon": "OVER_5_YEARS"}  # 6 -> MODERATE
HIGH_EDGE = {**MODERATE_TOP, "emergency_fund": "OVER_6_MONTHS"}  # 7 -> HIGH


def _put(client: TestClient, actor: Actor, body: dict[str, Any]) -> Any:
    return client.put(URL, headers=actor.headers, json=body)


def test_unassessed_profile_is_empty_not_invented(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    body = client.get(URL, headers=alice.headers).json()
    assert body == {
        "band": None,
        "score": None,
        "max_score": 10,
        "answers": None,
        "questionnaire_version": None,
        "assessed_at": None,
    }


@pytest.mark.parametrize(
    ("answers", "score", "band"),
    [
        (CAUTIOUS, 2, "LOW"),
        (LOW_EDGE, 3, "LOW"),
        (MODERATE_EDGE, 4, "MODERATE"),
        (MODERATE_TOP, 6, "MODERATE"),
        (HIGH_EDGE, 7, "HIGH"),
    ],
)
def test_questionnaire_produces_the_band_at_each_boundary(
    client: TestClient,
    make_customer: Callable[..., Actor],
    clock: FakeClock,
    answers: dict[str, str],
    score: int,
    band: str,
) -> None:
    alice = make_customer()
    clock.now = ASSESSED.timestamp()
    resp = _put(client, alice, answers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["band"], body["score"]) == (band, score)
    assert body["answers"] == answers
    assert body["questionnaire_version"] == "RTQ-1"
    assert datetime.fromisoformat(body["assessed_at"]) == ASSESSED
    assert client.get(URL, headers=alice.headers).json() == body  # persisted


def test_reassessment_replaces_band_answers_and_date_and_is_audited_each_time(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock, db: Session
) -> None:
    alice = make_customer()
    clock.now = ASSESSED.timestamp()
    assert _put(client, alice, CAUTIOUS).json()["band"] == "LOW"
    clock.advance(86_400)
    second = _put(client, alice, HIGH_EDGE).json()
    assert second["band"] == "HIGH" and second["answers"] == HIGH_EDGE
    assert datetime.fromisoformat(second["assessed_at"]).date().isoformat() == "2026-11-03"

    customer = db.get(Customer, alice.customer_id)
    assert customer is not None and customer.risk_profile == "HIGH"
    assert customer.risk_answers == HIGH_EDGE and customer.risk_score == 7

    rows = list(
        db.execute(
            select(AuditLog)
            .where(AuditLog.action == "RISK_PROFILE_ASSESSED")
            .order_by(AuditLog.seq)
        ).scalars()
    )
    assert [r.actor_user_id for r in rows] == [alice.user_id, alice.user_id]
    assert all(r.resource_type == "customer" for r in rows)
    assert all(r.resource_id == str(alice.customer_id) for r in rows)
    assert [(r.details["previous_band"], r.details["band"]) for r in rows] == [
        (None, "LOW"),
        ("LOW", "HIGH"),
    ]
    assert rows[1].details["answers"] == HIGH_EDGE
    assert rows[1].details["questionnaire_version"] == "RTQ-1"


@pytest.mark.parametrize(
    "body",
    [
        {},  # nothing answered
        {k: v for k, v in CAUTIOUS.items() if k != "emergency_fund"},  # one missing
        {**CAUTIOUS, "loss_reaction": "PANIC"},  # not an allowed answer
        {**CAUTIOUS, "investment_horizon": 2},  # points are not accepted, only answers
        {**CAUTIOUS, "band": "HIGH"},  # the band is computed, never supplied
        {**CAUTIOUS, "customer_id": "00000000-0000-0000-0000-000000000001"},
        {**CAUTIOUS, "investment_goal": "x" * 5000},
    ],
)
def test_invalid_questionnaires_are_rejected_and_nothing_is_stored(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, body: dict[str, Any]
) -> None:
    alice = make_customer()
    resp = _put(client, alice, body)
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
    assert "PANIC" not in resp.text and "xxxxx" not in resp.text  # input is never echoed
    assert client.get(URL, headers=alice.headers).json()["band"] is None
    assert (
        db.execute(select(AuditLog).where(AuditLog.action == "RISK_PROFILE_ASSESSED")).first()
        is None
    )


def test_each_customer_reads_and_writes_only_their_own_profile(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice, bob = make_customer(), make_customer()
    _put(client, alice, HIGH_EDGE)
    assert client.get(URL, headers=bob.headers).json()["band"] is None
    _put(client, bob, CAUTIOUS)
    assert client.get(URL, headers=alice.headers).json()["band"] == "HIGH"
    # a client-supplied customer id is not a parameter: it is ignored, never honoured
    sneaky = client.get(f"{URL}?customer_id={alice.customer_id}", headers=bob.headers).json()
    assert sneaky["band"] == "LOW"


def test_staff_and_anonymous_callers_have_no_risk_profile(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    assert client.get(URL).status_code == 401
    assert client.put(URL, json=CAUTIOUS).status_code == 401
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    assert client.get(URL, headers=rm.headers).status_code == 403
    assert client.put(URL, headers=rm.headers, json=CAUTIOUS).status_code == 403


def test_database_refuses_a_band_without_its_answers(
    make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    """Defence in depth: a band can only be stored with the score, answers and time behind it."""
    alice = make_customer()
    for statement in (
        "UPDATE customers SET risk_profile = 'HIGH' WHERE id = :id",
        "UPDATE customers SET risk_profile = 'EXTREME', risk_score = 5, risk_answers = '{}',"
        " risk_questionnaire_version = 'RTQ-1', risk_assessed_at = now() WHERE id = :id",
        "UPDATE customers SET risk_profile = 'LOW', risk_score = 11, risk_answers = '{}',"
        " risk_questionnaire_version = 'RTQ-1', risk_assessed_at = now() WHERE id = :id",
    ):
        with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
            connection.execute(text(statement), {"id": alice.customer_id})
