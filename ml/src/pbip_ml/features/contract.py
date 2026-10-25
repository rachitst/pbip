"""DRAFT customer behavioural feature contract (TASK-010, ADR 0010).

A feature vector is always computed *as of* an instant and may only use events that happened
strictly before it. That one rule is what prevents target leakage: a label (fraud, churn) is
observed at or after ``as_of``, so nothing derived from it - or from anything later - can enter
the features. The input schema has no label fields at all (``extra="forbid"``), so a label column
cannot be passed in by accident.

Status: draft. Nothing on the API request path consumes it yet; the Sprint 2 feature pipeline
implements it online (Redis/PostgreSQL) and must match ``pbip_ml.features.offline`` (parity tests).
Money is integer minor units; averages are rounded to the nearest paisa.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

FeatureSetVersion = Literal["customer-features-v0.1-draft"]
FEATURE_SET_VERSION: FeatureSetVersion = "customer-features-v0.1-draft"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


AwareDatetime = Annotated[datetime, AfterValidator(_require_aware)]


class FeatureSource(StrEnum):
    TRANSACTIONS = "TRANSACTIONS"
    SECURITY_EVENTS = "SECURITY_EVENTS"  # reserved: logins/devices (Sprint 3, F3)


class FeatureDefinition(BaseModel):
    """Catalogue entry: what a feature means, its window and type. Documentation as data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    dtype: Literal["int", "float"]
    window: str = Field(description="Look-back window ending strictly before as_of, e.g. 30d")
    unit: str
    description: str = Field(max_length=240)
    source: FeatureSource = FeatureSource.TRANSACTIONS


class TransactionEvent(BaseModel):
    """The only input the offline/online feature code may read. Deliberately label-free."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: UUID
    occurred_at: AwareDatetime
    direction: Literal["CREDIT", "DEBIT"]
    amount_minor: int = Field(gt=0)
    merchant: str = Field(min_length=1, max_length=80)


class CustomerFeatures(BaseModel):
    """One customer's behavioural features as of an instant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_set_version: FeatureSetVersion = FEATURE_SET_VERSION
    customer_id: UUID
    as_of: AwareDatetime
    avg_amount_30d: int = Field(ge=0, description="Mean debit amount, last 30 days (minor units)")
    max_amount_30d: int = Field(ge=0, description="Largest debit, last 30 days (minor units)")
    debit_count_30d: int = Field(ge=0)
    debit_total_30d: int = Field(ge=0, description="Sum of debits, last 30 days (minor units)")
    credit_total_30d: int = Field(ge=0, description="Sum of credits, last 30 days (minor units)")
    transfer_velocity_10m: int = Field(ge=0, description="Debits in the 10 minutes before as_of")
    transfer_velocity_24h: int = Field(ge=0, description="Debits in the 24 hours before as_of")
    distinct_merchants_30d: int = Field(ge=0)
    night_debit_ratio_30d: float = Field(ge=0, le=1, description="Share of debits 00:00-05:59 IST")
    days_since_last_txn: float | None = Field(
        default=None, ge=0, description="None when the customer has no prior activity"
    )


FEATURE_CATALOGUE: tuple[FeatureDefinition, ...] = (
    FeatureDefinition(
        name="avg_amount_30d", dtype="int", window="30d", unit="paise",
        description="Mean debit amount; baseline for amount-spike rules (F5).",
    ),
    FeatureDefinition(
        name="max_amount_30d", dtype="int", window="30d", unit="paise",
        description="Largest debit; caps what 'unusually large' means for this customer.",
    ),
    FeatureDefinition(
        name="debit_count_30d", dtype="int", window="30d", unit="count",
        description="Number of debits; activity level and dormancy signal (F8).",
    ),
    FeatureDefinition(
        name="debit_total_30d", dtype="int", window="30d", unit="paise",
        description="Total outflow; with credit_total_30d gives pass-through ratio (F6).",
    ),
    FeatureDefinition(
        name="credit_total_30d", dtype="int", window="30d", unit="paise",
        description="Total inflow (salary, receipts).",
    ),
    FeatureDefinition(
        name="transfer_velocity_10m", dtype="int", window="10m", unit="count",
        description="Debits in the last 10 minutes; velocity-burst rule (F2).",
    ),
    FeatureDefinition(
        name="transfer_velocity_24h", dtype="int", window="24h", unit="count",
        description="Debits in the last 24 hours; slower velocity context.",
    ),
    FeatureDefinition(
        name="distinct_merchants_30d", dtype="int", window="30d", unit="count",
        description="Merchant diversity; new-counterparty context (F1).",
    ),
    FeatureDefinition(
        name="night_debit_ratio_30d", dtype="float", window="30d", unit="ratio",
        description="Share of debits between 00:00 and 05:59 IST; unusual-hour context.",
    ),
    FeatureDefinition(
        name="days_since_last_txn", dtype="float", window="unbounded", unit="days",
        description="Recency; dormant-account activation signal (F8).",
    ),
)  # fmt: skip
