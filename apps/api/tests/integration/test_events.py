"""Integration tests for telemetry and clickstream event ingestion API (TASK-055).

Verifies:
- POST /api/v1/events/batch accepts telemetry events (page_view, loan_calculator_used, transfer_initiated)
- Monthly partitioned table clickstream_events stores events with partition routing
- Strict schema validation rejecting untyped or malformed payloads (422)
- Authentication and authorization (401 unauthenticated, 403 staff)
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings, get_settings
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.events.models import ClickstreamEvent
from pbip_api.modules.events.partitions import ensure_monthly_partitions
from pbip_test_support import Actor
from sqlalchemy import select, text
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_events_batch_happy_path(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
) -> None:
    """Customer submits batch of telemetry events and they are persisted in partitioned storage."""
    alice = make_customer()
    now_str = datetime.now(UTC).isoformat()

    batch_payload = {
        "events": [
            {
                "event_type": "page_view",
                "event_timestamp": now_str,
                "session_id": "sess-alpha-123",
                "page_url": "/dashboard",
                "properties": {"view_duration_ms": 4500},
            },
            {
                "event_type": "loan_calculator_used",
                "event_timestamp": now_str,
                "session_id": "sess-alpha-123",
                "page_url": "/loans/calculator",
                "properties": {"amount_minor": 50000000, "tenure_months": 24},
            },
            {
                "event_type": "transfer_initiated",
                "event_timestamp": now_str,
                "session_id": "sess-alpha-123",
                "page_url": "/transfers",
                "properties": {"amount_minor": 150000, "transfer_type": "DOMESTIC"},
            },
        ]
    }

    resp = client.post("/api/v1/events/batch", json=batch_payload, headers=alice.headers)

    assert resp.status_code == 202, resp.text

    data = resp.json()
    assert data["accepted"] == 3
    assert "batch_id" in data
    assert "received_at" in data

    # Verify rows in PostgreSQL partitioned table
    stmt = (
        select(ClickstreamEvent)
        .where(ClickstreamEvent.customer_id == alice.customer_id)
        .order_by(ClickstreamEvent.created_at.desc())
    )
    saved_events = list(db.scalars(stmt).all())
    assert len(saved_events) >= 3

    types = {e.event_type for e in saved_events}
    assert "page_view" in types
    assert "loan_calculator_used" in types
    assert "transfer_initiated" in types

    for ev in saved_events:
        assert ev.session_id == "sess-alpha-123"
        assert ev.customer_id == alice.customer_id


def test_events_batch_validation_failure(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Strict schema validation rejects empty batches and untyped event types."""
    alice = make_customer()

    # Empty events list rejected
    resp_empty = client.post(
        "/api/v1/events/batch",
        json={"events": []},
        headers=alice.headers,
    )
    assert resp_empty.status_code == 422

    # Unknown / untyped event type rejected
    resp_bad_type = client.post(
        "/api/v1/events/batch",
        json={
            "events": [
                {
                    "event_type": "random_unregistered_event_name",
                    "event_timestamp": datetime.now(UTC).isoformat(),
                    "session_id": "sess-1",
                }
            ]
        },
        headers=alice.headers,
    )
    assert resp_bad_type.status_code == 422


def test_events_batch_auth_and_staff_forbidden(
    client: TestClient,
    make_staff: Callable[..., Actor],
) -> None:
    """Unauthenticated returns 401; staff users return 403 (for customers only)."""
    now_str = datetime.now(UTC).isoformat()
    valid_payload = {
        "events": [
            {
                "event_type": "page_view",
                "event_timestamp": now_str,
                "session_id": "sess-1",
            }
        ]
    }

    # 401 Unauthenticated
    resp_unauth = client.post("/api/v1/events/batch", json=valid_payload)
    assert resp_unauth.status_code == 401

    # 403 Staff
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    resp_staff = client.post(
        "/api/v1/events/batch",
        json=valid_payload,
        headers=analyst.headers,
    )
    assert resp_staff.status_code == 403


def test_events_partition_routing_across_months(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
) -> None:
    """Events across different calendar months route cleanly to their monthly partitions."""
    alice = make_customer()

    # This month and last month: two monthly partitions, inside the 180-day window whenever it runs
    now = datetime.now(UTC)
    last_month = now.replace(day=1) - timedelta(days=1)
    events_payload = {
        "events": [
            {
                "event_type": "page_view",
                "event_timestamp": last_month.isoformat(),
                "session_id": "sess-oct",
                "page_url": "/accounts",
            },
            {
                "event_type": "product_clicked",
                "event_timestamp": now.isoformat(),
                "session_id": "sess-nov",
                "page_url": "/recommendations",
            },
        ]
    }

    resp = client.post("/api/v1/events/batch", json=events_payload, headers=alice.headers)
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 2

    # Query back to verify both partitions stored events
    stmt = (
        select(ClickstreamEvent)
        .where(ClickstreamEvent.customer_id == alice.customer_id)
        .where(ClickstreamEvent.session_id.in_(["sess-oct", "sess-nov"]))
    )
    events = list(db.scalars(stmt).all())
    assert len(events) == 2


def _event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_type": "page_view",
        "event_timestamp": datetime.now(UTC).isoformat(),
        "session_id": "sess-review",
        "page_url": "/accounts",
    }
    event.update(overrides)
    return event


@pytest.mark.parametrize(
    "bad_event",
    [
        {"customer_id": "00000000-0000-0000-0000-000000000001"},  # identity is server-side only
        {"user_id": "00000000-0000-0000-0000-000000000001"},
        {"properties": {"note": "call me at alice@example.com"}},  # free text
        {"properties": {"referrer": "https://example.com/?email=a@b.c"}},
        {"properties": {"nested": {"a": 1}}},
        {"page_url": "/accounts?email=alice@example.com"},  # query strings can carry PII
        {"page_url": "https://evil.example/accounts"},
        {"session_id": "has spaces and <script>"},
    ],
)
def test_events_reject_extra_fields_free_text_and_urls(
    client: TestClient, make_customer: Callable[..., Actor], bad_event: dict[str, object]
) -> None:
    alice = make_customer()
    resp = client.post(
        "/api/v1/events/batch", json={"events": [_event(**bad_event)]}, headers=alice.headers
    )
    assert resp.status_code == 422, resp.text


def test_events_reject_unknown_top_level_fields(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    resp = client.post(
        "/api/v1/events/batch",
        json={"events": [_event()], "customer_id": "00000000-0000-0000-0000-000000000001"},
        headers=alice.headers,
    )
    assert resp.status_code == 422


def test_events_resend_is_idempotent_not_a_server_error(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    event = _event(event_id="7b0c1f3e-1111-4c1e-9d1e-2a2b2c2d2e2f")
    first = client.post("/api/v1/events/batch", json={"events": [event]}, headers=alice.headers)
    again = client.post("/api/v1/events/batch", json={"events": [event]}, headers=alice.headers)
    assert first.status_code == again.status_code == 202
    assert (first.json()["accepted"], first.json()["duplicates"]) == (1, 0)
    assert (again.json()["accepted"], again.json()["duplicates"]) == (0, 1)
    stored = db.scalars(
        select(ClickstreamEvent).where(ClickstreamEvent.customer_id == alice.customer_id)
    ).all()
    assert len(stored) == 1


def test_events_are_rate_limited(
    app: FastAPI, client: TestClient, make_customer: Callable[..., Actor], settings: Settings
) -> None:
    alice = make_customer()
    tight = settings.model_copy(update={"rate_limit_events_per_minute": 2})
    app.dependency_overrides[get_settings] = lambda: tight
    body = {"events": [_event()]}
    codes = [
        client.post("/api/v1/events/batch", json=body, headers=alice.headers).status_code
        for _ in range(3)
    ]
    assert codes == [202, 202, 429]


def test_no_ip_address_or_user_agent_is_stored(db: Session) -> None:
    columns = set(
        db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'clickstream_events'"
            )
        ).scalars()
    )
    assert "ip_address" not in columns and "user_agent" not in columns
    assert {"customer_id", "event_type", "event_timestamp"} <= columns


def test_partition_job_creates_future_months_so_events_avoid_the_default_partition(
    db: Session,
) -> None:
    created = ensure_monthly_partitions(db, date(2027, 4, 15), months_ahead=2)
    db.commit()
    try:
        assert created == [
            "clickstream_events_2027_04",
            "clickstream_events_2027_05",
            "clickstream_events_2027_06",
        ]
        assert ensure_monthly_partitions(db, date(2027, 4, 15), months_ahead=2) == []  # idempotent
        db.execute(
            text(
                "INSERT INTO clickstream_events (id, event_timestamp, event_type, session_id) "
                "VALUES (gen_random_uuid(), '2027-05-10T12:00:00Z', 'page_view', 'sess-job')"
            )
        )
        partition = db.execute(
            text(
                "SELECT tableoid::regclass::text FROM clickstream_events "
                "WHERE session_id = 'sess-job'"
            )
        ).scalar_one()
        assert partition == "clickstream_events_2027_05"
    finally:
        db.rollback()
        for name in ("2027_04", "2027_05", "2027_06"):
            db.execute(text(f"DROP TABLE IF EXISTS clickstream_events_{name}"))
        db.commit()
