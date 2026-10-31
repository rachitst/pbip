"""Pydantic schemas for clickstream and telemetry ingestion (TASK-055).

Events are analytics signals, not a place for personal data: unknown fields are rejected, the page
is a route path (no query string or fragment, which can carry emails or tokens), and ``properties``
holds only numbers, booleans and short identifier-like strings, never free text.
"""

import re
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pbip_api.core.db import utcnow

MAX_BATCH_EVENTS = 100
MAX_PROPERTIES = 20

_PROPERTY_KEY = r"^[a-z][a-z0-9_]{0,39}$"
_IDENTIFIER = r"^[A-Za-z0-9_.:-]{1,64}$"
_PROPERTY_KEY_RE = re.compile(_PROPERTY_KEY)
_IDENTIFIER_RE = re.compile(_IDENTIFIER)

PropertyValue = bool | int | float | str


class TelemetryEventType(StrEnum):
    """Supported telemetry event types."""

    PAGE_VIEW = "page_view"
    LOAN_CALCULATOR_USED = "loan_calculator_used"
    TRANSFER_INITIATED = "transfer_initiated"
    ACCOUNT_VIEWED = "account_viewed"
    PRODUCT_CLICKED = "product_clicked"
    SEARCH_PERFORMED = "search_performed"


class TelemetryEventIn(BaseModel):
    """One client-side or server-side telemetry event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID | None = Field(
        default=None, description="Client-generated id; resending the same event is a no-op"
    )
    event_type: TelemetryEventType = Field(..., description="Classification category of the event")
    event_timestamp: datetime = Field(..., description="UTC timestamp when the event occurred")
    session_id: str = Field(
        ..., pattern=r"^[A-Za-z0-9._-]{1,128}$", description="Opaque browser session identifier"
    )
    page_url: str | None = Field(
        default=None,
        max_length=256,
        pattern=r"^/[A-Za-z0-9/_.-]*$",
        description="Route path only, e.g. /accounts (no query string or fragment)",
    )
    properties: dict[str, PropertyValue] = Field(
        default_factory=dict,
        description=(
            f"Up to {MAX_PROPERTIES} numeric, boolean or identifier-like values (keys snake_case, "
            "strings up to 64 characters of letters, digits and _.:-); no free text"
        ),
    )

    @field_validator("event_timestamp")
    @classmethod
    def validate_timestamp_window(cls, v: datetime) -> datetime:
        """Timestamp must be within a day ahead and 180 days back (naive values are UTC)."""
        if v.tzinfo is None:
            v = v.replace(tzinfo=utcnow().tzinfo)
        now = utcnow()
        if v > now + timedelta(days=1):
            raise ValueError("event_timestamp cannot be more than 24 hours in the future")
        if v < now - timedelta(days=180):
            raise ValueError("event_timestamp cannot be older than 180 days")
        return v

    @field_validator("properties")
    @classmethod
    def validate_properties(cls, v: dict[str, PropertyValue]) -> dict[str, PropertyValue]:
        """Bounded, structured metadata only: no nested objects and no free-text strings."""
        if len(v) > MAX_PROPERTIES:
            raise ValueError(f"properties cannot exceed {MAX_PROPERTIES} keys")
        for key, val in v.items():
            if not _PROPERTY_KEY_RE.match(key):
                raise ValueError("property keys must be snake_case, at most 40 characters")
            if isinstance(val, str) and not _IDENTIFIER_RE.match(val):
                raise ValueError(
                    "property strings must be identifiers (letters, digits, _.:-; at most 64)"
                )
        return v


class EventBatchRequest(BaseModel):
    """Batch payload containing up to 100 telemetry events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: list[TelemetryEventIn] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_EVENTS,
        description=f"Telemetry events to ingest (1 to {MAX_BATCH_EVENTS} per batch)",
    )


class EventBatchResponse(BaseModel):
    """Result of batch event ingestion. The events are committed before this is returned."""

    model_config = ConfigDict(frozen=True)

    accepted: int = Field(..., ge=0, description="Events stored by this request")
    duplicates: int = Field(
        ..., ge=0, description="Events already stored (same event_id and timestamp), skipped"
    )
    batch_id: UUID = Field(default_factory=uuid4, description="Unique batch tracking UUID")
    received_at: datetime = Field(..., description="UTC server timestamp of receipt")
