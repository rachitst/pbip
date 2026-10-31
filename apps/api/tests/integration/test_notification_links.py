"""Notification link targets and security notifications (TASK-022 backend gap, #45)."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_api.modules.notifications.service import relay_outbox
from pbip_test_support import PASSWORD, Actor, age_payee
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

NEW_PASSWORD = f"{uuid.uuid4()}Aa1!"


def _relay(db: Session) -> None:
    relay_outbox(db)
    db.commit()


def _mine(client: TestClient, actor: Actor) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = client.get("/api/v1/notifications", headers=actor.headers).json()[
        "items"
    ]
    return items


def test_transfer_notification_links_its_transaction_and_account(
    client: TestClient,
    db: Session,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, bob = make_customer(funds=100_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    resp = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 500,
            "payee_id": payee["id"],
        },
    )
    assert resp.status_code == 201, resp.text
    _relay(db)
    (item,) = _mine(client, alice)
    assert item["resource_type"] == "transaction"
    assert item["resource_id"] == resp.json()["id"]
    assert item["account_id"] == str(alice.account_id)
    assert _mine(client, bob) == []  # the payee gets no copy of the sender's notification


def test_password_change_creates_a_security_notification(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    resp = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 204, resp.text
    _relay(db)
    rows = db.scalars(select(Notification).where(Notification.user_id == alice.user_id)).all()
    assert [(n.type, n.title, n.resource_type, n.resource_id) for n in rows] == [
        ("PASSWORD_CHANGED", "Password changed", "security", None)
    ]


def test_mfa_enrollment_creates_a_security_notification(
    db: Session, make_staff: Callable[..., Actor]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST, mfa=True)  # enrolment confirms MFA
    _relay(db)
    rows = db.scalars(select(Notification).where(Notification.user_id == analyst.user_id)).all()
    assert [(n.type, n.resource_type) for n in rows] == [("MFA_ENABLED", "security")]


def test_only_a_new_device_creates_a_security_notification(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    body = {"fingerprint": "device-fingerprint-1", "label": "Laptop"}
    assert client.post(
        "/api/v1/customers/devices", headers=alice.headers, json=body
    ).status_code in (
        200,
        201,
    )
    assert client.post(
        "/api/v1/customers/devices", headers=alice.headers, json=body
    ).status_code in (
        200,
        201,
    )  # the same device again: touched, not new
    _relay(db)
    assert [(n["type"], n["resource_type"]) for n in _mine(client, alice)] == [
        ("DEVICE_REGISTERED", "security")
    ]


def test_transfer_events_from_before_links_still_relay(
    db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    db.add(
        OutboxEvent(
            event_type=notifications.TRANSFER_POSTED,
            payload={"user_id": str(alice.user_id), "amount_minor": 500, "currency": "INR"},
        )
    )
    db.commit()
    _relay(db)
    (row,) = db.scalars(select(Notification).where(Notification.user_id == alice.user_id)).all()
    assert (row.resource_type, row.resource_id, row.account_id) == ("transaction", None, None)


def test_only_known_security_types_can_be_enqueued(db: Session) -> None:
    with pytest.raises(ValueError, match="not a security notification type"):
        notifications.enqueue_security(db, "SOMETHING_ELSE", uuid.uuid4())
