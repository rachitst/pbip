"""Pydantic schemas for customer profile and device registration."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from pbip_api.core.validation import EncodableText, Text


class CustomerProfileOut(BaseModel):
    """Authenticated customer profile details. Sensitive PII like phone is masked."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    email: str | None = None
    full_name: str
    date_of_birth: date | None = None
    phone_masked: str | None = None
    kyc_status: str
    created_at: datetime


class DeviceRegisterRequest(BaseModel):
    """Device registration payload. The raw fingerprint is never stored or logged in plain text."""

    fingerprint: EncodableText | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="Raw client device fingerprint string or hardware identifier",
    )
    label: Text | None = Field(
        default=None,
        max_length=100,
        description="Optional human-readable label for the device",
    )


class DeviceOut(BaseModel):
    """Device metadata returned to client.

    Raw fingerprints and fingerprint hashes are never exposed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    label: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    trusted: bool
    device_age_days: int
