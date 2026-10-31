"""Unit tests for clickstream and telemetry events ingestion (TASK-055)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pbip_api.modules.events.schemas import (
    EventBatchRequest,
    EventBatchResponse,
    TelemetryEventIn,
    TelemetryEventType,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_telemetry_event_types_supported() -> None:
    """Verify core telemetry event types are present and supported."""
    assert TelemetryEventType.PAGE_VIEW == "page_view"
    assert TelemetryEventType.LOAN_CALCULATOR_USED == "loan_calculator_used"
    assert TelemetryEventType.TRANSFER_INITIATED == "transfer_initiated"
    assert TelemetryEventType.ACCOUNT_VIEWED == "account_viewed"
    assert TelemetryEventType.PRODUCT_CLICKED == "product_clicked"


def test_valid_telemetry_event_in() -> None:
    """Valid telemetry event item parses correctly with defaults."""
    now = datetime.now(UTC)
    item = TelemetryEventIn(
        event_type=TelemetryEventType.LOAN_CALCULATOR_USED,
        event_timestamp=now,
        session_id="session-xyz-123",
        page_url="/loans/calculator",
        properties={"principal_minor": 50000000, "tenure_months": 36, "interest_rate": 10.5},
    )
    assert item.event_id is None
    assert item.event_type == TelemetryEventType.LOAN_CALCULATOR_USED
    assert item.event_timestamp == now
    assert item.session_id == "session-xyz-123"
    assert item.page_url == "/loans/calculator"
    assert item.properties["tenure_months"] == 36


def test_reject_untyped_or_unknown_event_type() -> None:
    """Strict schema validation rejects untyped or unknown event types."""
    now = datetime.now(UTC)
    with pytest.raises(ValidationError) as exc:
        TelemetryEventIn.model_validate(
            {
                "event_type": "arbitrary_untyped_hack",
                "event_timestamp": now.isoformat(),
                "session_id": "session-1",
            }
        )
    assert "event_type" in str(exc.value)


def test_reject_empty_session_id() -> None:
    """Session id cannot be empty."""
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        TelemetryEventIn(
            event_type=TelemetryEventType.PAGE_VIEW,
            event_timestamp=now,
            session_id="",
        )


def test_reject_future_timestamp() -> None:
    """Timestamps more than 24 hours in the future must be rejected."""
    future = datetime.now(UTC) + timedelta(days=2)
    with pytest.raises(ValidationError) as exc:
        TelemetryEventIn(
            event_type=TelemetryEventType.PAGE_VIEW,
            event_timestamp=future,
            session_id="session-1",
        )
    assert "future" in str(exc.value)


def test_reject_ancient_timestamp() -> None:
    """Timestamps older than 180 days must be rejected."""
    past = datetime.now(UTC) - timedelta(days=200)
    with pytest.raises(ValidationError) as exc:
        TelemetryEventIn(
            event_type=TelemetryEventType.PAGE_VIEW,
            event_timestamp=past,
            session_id="session-1",
        )
    assert "older than 180 days" in str(exc.value)


def test_reject_properties_with_excessive_keys() -> None:
    """Properties dict with more than 50 keys must be rejected."""
    now = datetime.now(UTC)
    excessive_props = {f"k_{i}": i for i in range(55)}
    with pytest.raises(ValidationError) as exc:
        TelemetryEventIn(
            event_type=TelemetryEventType.PAGE_VIEW,
            event_timestamp=now,
            session_id="session-1",
            properties=excessive_props,
        )
    assert "cannot exceed 20 keys" in str(exc.value)  # review: at most 20 structured values


def test_reject_oversized_property_value_string() -> None:
    """Long strings are free text, not identifiers: rejected (review: no free text in events)."""
    now = datetime.now(UTC)
    with pytest.raises(ValidationError) as exc:
        TelemetryEventIn(
            event_type=TelemetryEventType.PAGE_VIEW,
            event_timestamp=now,
            session_id="session-1",
            properties={"large_payload": "a" * 1500},
        )
    assert "property strings must be identifiers" in str(exc.value)


def test_event_batch_request_bounds() -> None:
    """Batch must contain between 1 and 100 events."""
    now = datetime.now(UTC)
    single_event = TelemetryEventIn(
        event_type=TelemetryEventType.TRANSFER_INITIATED,
        event_timestamp=now,
        session_id="sess-1",
        page_url="/transfers",
    )

    # Empty batch rejected
    with pytest.raises(ValidationError):
        EventBatchRequest(events=[])

    # 1 event accepted
    req = EventBatchRequest(events=[single_event])
    assert len(req.events) == 1

    # Over 100 events rejected
    with pytest.raises(ValidationError):
        EventBatchRequest(events=[single_event] * 101)


def test_event_batch_response_model() -> None:
    """Event batch response format and validation."""
    now = datetime.now(UTC)
    batch_id = uuid4()
    resp = EventBatchResponse(accepted=5, duplicates=0, batch_id=batch_id, received_at=now)
    assert resp.accepted == 5
    assert resp.batch_id == batch_id
    assert resp.received_at == now
