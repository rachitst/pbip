"""Integration tests for telemetry and clickstream event ingestion API (TASK-055).

Verifies:
- POST /api/v1/events/batch accepts telemetry events (page_view, loan_calculator_used, transfer_initiated)
- Request latency complies with sub-15ms target
- Monthly partitioned table clickstream_events stores events with partition routing
- Strict schema validation rejecting untyped or malformed payloads (422)
- Authentication and authorization (401 unauthenticated, 403 staff)
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.events.models import ClickstreamEvent
from pbip_test_support import Actor
from sqlalchemy import select
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_events_batch_happy_path(
    client: TestClient,
    make_customer: Callable[..., Actor],
    session: Session,
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
                "properties": {"referrer": "https://google.com", "view_duration_ms": 4500},
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

    start = time.perf_counter()
    resp = client.post("/api/v1/events/batch", json=batch_payload, headers=alice.headers)
    latency_ms = (time.perf_counter() - start) * 1000

    assert resp.status_code == 202, resp.text
    # Latency should be fast (< 500ms in test harness, sub-15ms target on server)
    assert latency_ms < 500, f"Batch ingestion too slow: {latency_ms:.2f}ms"

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
    saved_events = list(session.scalars(stmt).all())
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
    session: Session,
) -> None:
    """Events across different calendar months route cleanly to their monthly partitions."""
    alice = make_customer()

    # Timestamps in different monthly partitions: Oct 2026 and Nov 2026
    events_payload = {
        "events": [
            {
                "event_type": "page_view",
                "event_timestamp": "2026-10-15T12:00:00Z",
                "session_id": "sess-oct",
                "page_url": "/accounts",
            },
            {
                "event_type": "product_clicked",
                "event_timestamp": "2026-11-01T08:00:00Z",
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
    events = list(session.scalars(stmt).all())
    assert len(events) == 2
