"""Pydantic schemas for clickstream and telemetry ingestion (TASK-055)."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TelemetryEventType(StrEnum):
    """Supported telemetry event types."""

    PAGE_VIEW = "page_view"
    LOAN_CALCULATOR_USED = "loan_calculator_used"
    TRANSFER_INITIATED = "transfer_initiated"
    ACCOUNT_VIEWED = "account_viewed"
    PRODUCT_CLICKED = "product_clicked"
    SEARCH_PERFORMED = "search_performed"


class TelemetryEventIn(BaseModel):
    """Individual client-side or server-side telemetry event payload."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID | None = Field(default=None, description="Optional client-generated event UUID")
    event_type: TelemetryEventType = Field(..., description="Classification category of the event")
    event_timestamp: datetime = Field(..., description="UTC timestamp when the event occurred")
    session_id: str = Field(
        ..., min_length=1, max_length=128, description="User session token or browser identifier"
    )
    page_url: str | None = Field(
        default=None, max_length=1024, description="Application route or URL path"
    )
    properties: dict[str, Any] = Field(
        default_factory=dict, description="Arbitrary event metadata properties"
    )

    @field_validator("event_timestamp")
    @classmethod
    def validate_timestamp_window(cls, v: datetime) -> datetime:
        """Timestamp must be timezone-aware and within reasonable temporal bounds."""
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        max_future = now + timedelta(days=1)
        max_past = now - timedelta(days=180)

        if v > max_future:
            raise ValueError("event_timestamp cannot be more than 24 hours in the future")
        if v < max_past:
            raise ValueError("event_timestamp cannot be older than 180 days")
        return v

    @field_validator("properties")
    @classmethod
    def validate_properties(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Bounded properties size to prevent Denial of Service and payload bloating."""
        if len(v) > 50:
            raise ValueError("properties dictionary cannot exceed 50 keys")
        for key, val in v.items():
            if len(key) > 64:
                raise ValueError(f"property key '{key[:16]}...' exceeds 64 characters limit")
            if isinstance(val, str) and len(val) > 1000:
                raise ValueError(f"property value for key '{key}' exceeds 1000 characters limit")
        return v


class EventBatchRequest(BaseModel):
    """Batch payload containing up to 100 telemetry events."""

    model_config = ConfigDict(frozen=True)

    events: list[TelemetryEventIn] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="List of telemetry events to ingest (1 to 100 items per batch)",
    )


class EventBatchResponse(BaseModel):
    """Result of batch event ingestion."""

    model_config = ConfigDict(frozen=True)

    accepted: int = Field(..., ge=0, description="Count of accepted events in batch")
    batch_id: UUID = Field(default_factory=uuid4, description="Unique batch tracking UUID")
    received_at: datetime = Field(..., description="UTC server timestamp of receipt")
