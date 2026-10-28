"""Integration tests for customer notification inbox, read tracking, and BOLA guarantees."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.notifications.models import Notification
from pbip_test_support import Actor
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def _seed_notification(
    db: Session,
    user_id: uuid.UUID,
    title: str = "Test notification",
    body: str = "Details of notification",
    notification_type: str = "TEST_EVENT",
    is_read: bool = False,
) -> Notification:
    notif = Notification(
        user_id=user_id,
        type=notification_type,
        title=title,
        body=body,
        read_at=db.bind.execute(  # type: ignore[union-attr]
            pytest.importorskip("sqlalchemy").text("now()")
        ).scalar()
        if is_read
        else None,
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif


def test_list_notifications_empty_inbox(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    resp = client.get("/api/v1/notifications", headers=alice.headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["unread_count"] == 0
    assert data["limit"] == 50
    assert data["offset"] == 0


def test_list_notifications_pagination_and_unread_filtering(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    # Create 5 notifications: 3 unread, 2 read
    for i in range(3):
        _seed_notification(db, alice.user_id, title=f"Unread {i}", is_read=False)
    for i in range(2):
        _seed_notification(db, alice.user_id, title=f"Read {i}", is_read=True)

    # 1. Full list default pagination
    resp = client.get("/api/v1/notifications", headers=alice.headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 5
    assert data["total"] == 5
    assert data["unread_count"] == 3

    # 2. Limit and offset
    page1 = client.get("/api/v1/notifications?limit=2&offset=0", headers=alice.headers).json()
    assert len(page1["items"]) == 2
    assert page1["total"] == 5
    assert page1["unread_count"] == 3
    assert page1["limit"] == 2
    assert page1["offset"] == 0

    page2 = client.get("/api/v1/notifications?limit=2&offset=2", headers=alice.headers).json()
    assert len(page2["items"]) == 2
    assert page2["items"][0]["id"] != page1["items"][0]["id"]

    page3 = client.get("/api/v1/notifications?limit=2&offset=4", headers=alice.headers).json()
    assert len(page3["items"]) == 1

    # 3. unread_only filter
    unread_resp = client.get("/api/v1/notifications?unread_only=true", headers=alice.headers).json()
    assert len(unread_resp["items"]) == 3
    assert unread_resp["total"] == 3
    assert unread_resp["unread_count"] == 3
    assert all(not item["read"] for item in unread_resp["items"])


def test_mark_individual_notification_read(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    n = _seed_notification(db, alice.user_id, title="Transfer Alert", is_read=False)

    # Mark as read
    res = client.post(f"/api/v1/notifications/{n.id}/read", headers=alice.headers)
    assert res.status_code == 204

    # Verify updated read status and unread_count
    inbox = client.get("/api/v1/notifications", headers=alice.headers).json()
    assert inbox["items"][0]["id"] == str(n.id)
    assert inbox["items"][0]["read"] is True
    assert inbox["unread_count"] == 0

    # Idempotent: marking already-read notification returns 204
    res_repeat = client.post(f"/api/v1/notifications/{n.id}/read", headers=alice.headers)
    assert res_repeat.status_code == 204


def test_mark_all_read_bulk_updates_only_owners_notifications(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice, bob = make_customer(), make_customer()
    for i in range(3):
        _seed_notification(db, alice.user_id, title=f"Alice unread {i}", is_read=False)
    for i in range(2):
        _seed_notification(db, bob.user_id, title=f"Bob unread {i}", is_read=False)

    # Bulk mark all read for Alice
    res = client.post("/api/v1/notifications/mark-all-read", headers=alice.headers)
    assert res.status_code == 200
    assert res.json() == {"updated_count": 3}

    # Alice has 0 unread now
    alice_inbox = client.get("/api/v1/notifications", headers=alice.headers).json()
    assert alice_inbox["unread_count"] == 0
    assert all(item["read"] for item in alice_inbox["items"])

    # Bob's unread notifications are completely untouched
    bob_inbox = client.get("/api/v1/notifications", headers=bob.headers).json()
    assert bob_inbox["unread_count"] == 2
    assert all(not item["read"] for item in bob_inbox["items"])

    # Calling mark-all-read again when all are read returns updated_count: 0
    repeat_res = client.post("/api/v1/notifications/mark-all-read", headers=alice.headers)
    assert repeat_res.status_code == 200
    assert repeat_res.json() == {"updated_count": 0}


def test_bola_cannot_read_or_mark_other_users_notifications(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice, mallory = make_customer(), make_customer()
    alice_notif = _seed_notification(db, alice.user_id, title="Alice Secret", is_read=False)

    # Mallory cannot mark Alice's notification as read -> 404 (non-existent/non-owned indistinguishable)
    res = client.post(f"/api/v1/notifications/{alice_notif.id}/read", headers=mallory.headers)
    assert res.status_code == 404
    assert res.json()["code"] == "not_found"

    # Mallory querying random UUID notification also gets 404
    random_id = uuid.uuid4()
    assert (
        client.post(f"/api/v1/notifications/{random_id}/read", headers=mallory.headers).status_code
        == 404
    )

    # Alice's notification remains unread
    alice_inbox = client.get("/api/v1/notifications", headers=alice.headers).json()
    assert alice_inbox["unread_count"] == 1
    assert alice_inbox["items"][0]["read"] is False

    # Mallory does not see Alice's notification in listings
    mallory_inbox = client.get("/api/v1/notifications", headers=mallory.headers).json()
    assert mallory_inbox["total"] == 0
    assert mallory_inbox["items"] == []


def test_unauthenticated_requests_are_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/notifications").status_code == 401
    assert client.post(f"/api/v1/notifications/{uuid.uuid4()}/read").status_code == 401
    assert client.post("/api/v1/notifications/mark-all-read").status_code == 401
