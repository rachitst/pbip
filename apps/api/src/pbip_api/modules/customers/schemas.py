"""Pydantic schemas for customer profile and device registration."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pbip_api.core.validation import EncodableText, Text
from pbip_api.modules.customers.demographics import AgeBand
from pbip_api.modules.customers.risk import (
    MAX_SCORE,
    EmergencyFund,
    InvestmentExperience,
    InvestmentGoal,
    InvestmentHorizon,
    LossReaction,
    RiskBand,
)


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


class RiskQuestionnaire(BaseModel):
    """The five risk-tolerance questions (TASK-097). Every answer is an enumerated choice."""

    model_config = ConfigDict(extra="forbid")

    investment_horizon: InvestmentHorizon = Field(
        description="How long you expect to keep money invested"
    )
    loss_reaction: LossReaction = Field(
        description="What you would do if your investments fell 20% in a year"
    )
    investment_goal: InvestmentGoal = Field(description="What matters most for your investments")
    investment_experience: InvestmentExperience = Field(
        description="Your experience of market-linked investments"
    )
    emergency_fund: EmergencyFund = Field(
        description="Months of essential spending you could cover without investments"
    )


class RiskProfileOut(BaseModel):
    """The customer's current risk band and the answers that produced it; nulls until assessed."""

    band: RiskBand | None = Field(description="LOW, MODERATE or HIGH; null if never assessed")
    score: int | None = Field(ge=0, le=MAX_SCORE, description="Sum of the answer points")
    max_score: int = Field(description="Highest possible score")
    answers: RiskQuestionnaire | None
    questionnaire_version: str | None = Field(
        description="Questionnaire version that scored these answers"
    )
    assessed_at: datetime | None = Field(description="When the band was last assessed")


class DateOfBirthIn(BaseModel):
    """Self-declared date of birth (ADR-0015). The age must be 18 to 120 on the bank's date."""

    model_config = ConfigDict(extra="forbid")

    date_of_birth: date = Field(description="ISO date, YYYY-MM-DD")

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _iso_text_only(cls, value: object) -> object:
        # Pydantic would also take epoch numbers; a date of birth is only ever a written date.
        if not isinstance(value, str) or len(value) != len("YYYY-MM-DD"):
            raise ValueError("date of birth must be an ISO date, YYYY-MM-DD")
        return value


class RecommendationsConsentIn(BaseModel):
    """Grant (true) or withdraw (false) consent to use the date of birth for recommendations."""

    model_config = ConfigDict(extra="forbid")

    granted: bool = Field(strict=True)


class DemographicsOut(BaseModel):
    """The customer's own date of birth and consent state. Returned to the owner only."""

    date_of_birth: date | None
    age_band: AgeBand = Field(description="Band derived from the date of birth today")
    recommendations_consent: bool = Field(
        description="Whether the age band may be used to tailor product recommendations"
    )
    consent_granted_at: datetime | None
    consent_policy_version: str | None = Field(
        description="Policy version the current consent was given under"
    )
    current_policy_version: str = Field(description="Policy version a new consent agrees to")
