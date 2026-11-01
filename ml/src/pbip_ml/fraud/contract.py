"""The fraud-scoring contract between the banking platform (A) and the fraud engine (B).

The platform builds a ``TransactionContext`` and calls ``FraudScorer.score``. How features are
computed (velocity counters, customer baselines, device history) is the scorer's business.
The result is ALWAYS explainable: a decision, a 0-100 score, human-readable reasons and the
model version that produced it. There is no black-box path.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FraudDecision(StrEnum):
    ALLOW = "ALLOW"
    STEP_UP = "STEP_UP"  # require an extra authentication factor before posting
    REVIEW = "REVIEW"  # hold funds, queue for a fraud analyst
    BLOCK = "BLOCK"  # decline


class ReasonSource(StrEnum):
    RULE = "RULE"
    MODEL = "MODEL"
    ANOMALY = "ANOMALY"


class FraudReason(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(
        min_length=1, max_length=64, description="Stable machine code, e.g. NEW_PAYEE"
    )
    message: str = Field(min_length=1, max_length=240, description="Analyst-readable explanation")
    source: ReasonSource
    contribution: float | None = Field(
        default=None, description="Signed contribution to the score (e.g. SHAP value) if known"
    )


class TransactionContext(BaseModel):
    """Everything the platform knows at the moment a transaction is submitted."""

    model_config = ConfigDict(frozen=True)

    transaction_id: UUID
    customer_id: UUID
    account_id: UUID
    amount_minor: int = Field(gt=0, description="Integer minor units (paise)")
    currency: str = Field(min_length=3, max_length=3)
    occurred_at: datetime
    channel: Literal["WEB", "MOBILE", "API"] = "WEB"
    payee_id: UUID | None = None
    device_id: UUID | None = None
    ip_country: str | None = Field(default=None, min_length=2, max_length=2)
    ip_network: str | None = Field(
        default=None,
        max_length=43,
        description="Client network prefix (IPv4 /24, IPv6 /48), never the full address (ADR-0020)",
    )
    geo_lat: float | None = Field(default=None, ge=-90, le=90)
    geo_lon: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware (UTC)")
        return value


class FraudScoreResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    risk_score: int = Field(ge=0, le=100)
    decision: FraudDecision
    reasons: list[FraudReason]
    model_version: str
    latency_ms: float = Field(ge=0)
    features: dict[str, float | int | str | bool | None] = Field(
        default_factory=dict, description="Feature snapshot used for this decision (audit trail)"
    )
    scored_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FraudScorer(Protocol):
    def score(self, context: TransactionContext) -> FraudScoreResult:
        """Score one transaction. Must be side-effect free and complete well under 100 ms."""
        ...
