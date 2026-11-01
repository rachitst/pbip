"""Pydantic schemas for fraud analytics, telemetry, and decision statistics (TASK-035)."""

from uuid import UUID

from pydantic import BaseModel, Field


class DecisionSummary(BaseModel):
    """Aggregate decision counts and rates across the analyzed period."""

    allow: int = Field(ge=0, description="Total transactions allowed")
    step_up: int = Field(ge=0, description="Total transactions requiring step-up MFA challenge")
    review: int = Field(ge=0, description="Total transactions held for fraud analyst review")
    block: int = Field(ge=0, description="Total transactions declined outright")
    total: int = Field(ge=0, description="Total decisions scored in period")
    hold_rate: float = Field(
        ge=0.0, le=1.0, description="Proportion of transactions held for review"
    )
    block_rate: float = Field(ge=0.0, le=1.0, description="Proportion of transactions blocked")
    step_up_rate: float = Field(ge=0.0, le=1.0, description="Proportion of transactions challenged")


class DailyDecisionPoint(BaseModel):
    """Decision counts for a single calendar day (YYYY-MM-DD)."""

    date: str = Field(description="Calendar date in ISO format (YYYY-MM-DD)")
    allow: int = Field(ge=0, description="ALLOW count for this day")
    step_up: int = Field(ge=0, description="STEP_UP count for this day")
    review: int = Field(ge=0, description="REVIEW count for this day")
    block: int = Field(ge=0, description="BLOCK count for this day")
    total: int = Field(ge=0, description="Total decisions on this day")


class TriggeredRuleStat(BaseModel):
    """Frequency and prevalence of an individual fraud detection rule."""

    rule_code: str = Field(
        description="Machine-readable rule identifier (e.g. HIGH_AMOUNT, VELOCITY)"
    )
    count: int = Field(ge=0, description="Number of events that triggered this rule")
    percentage: float = Field(
        ge=0.0, le=100.0, description="Percentage of total decisions that triggered this rule"
    )


class HighRiskPayeeStat(BaseModel):
    """Payee with suspicious or repeatedly flagged transaction history."""

    payee_id: UUID = Field(description="Payee unique identifier")
    payee_name: str = Field(description="Payee legal name")
    bank_code: str = Field(description="Destination bank or IFSC code")
    flagged_count: int = Field(
        ge=0, description="Count of flagged transactions directed to this payee"
    )
    avg_risk_score: float = Field(
        ge=0.0, le=100.0, description="Average risk score across transactions to this payee"
    )


class FraudStatsResponse(BaseModel):
    """Comprehensive telemetry report for fraud analysts and risk operations."""

    period_days: int = Field(ge=1, description="Analyzed lookback window in days")
    total_decisions: int = Field(ge=0, description="Total decisions scored across the period")
    decision_counts: dict[str, int] = Field(
        description="Decision counts keyed by ALLOW, STEP_UP, REVIEW, BLOCK"
    )
    hold_rate: float = Field(ge=0.0, le=1.0, description="Overall hold rate for the period")
    block_rate: float = Field(ge=0.0, le=1.0, description="Overall block rate for the period")
    summary: DecisionSummary = Field(
        description="Detailed summary of decision counts and proportions"
    )
    timeline: list[DailyDecisionPoint] = Field(
        description="Chronological daily decision volume over time"
    )
    rules_breakdown: list[TriggeredRuleStat] = Field(
        description="Most frequently triggered detection rules ranked by frequency"
    )
    high_risk_payees: list[HighRiskPayeeStat] = Field(
        description="Payees associated with flagged transactions or elevated risk scores"
    )
