"""Forecast contract. Forecasts always carry an interval: we never pretend a prediction is exact."""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ForecastKind(StrEnum):
    MONTHLY_SPEND = "MONTHLY_SPEND"
    END_OF_MONTH_BALANCE = "END_OF_MONTH_BALANCE"
    FRAUD_VOLUME = "FRAUD_VOLUME"


class ForecastPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    period_start: date
    lower: float
    median: float
    upper: float

    @model_validator(mode="after")
    def _ordered(self) -> "ForecastPoint":
        if not (self.lower <= self.median <= self.upper):
            raise ValueError("require lower <= median <= upper")
        return self


class Forecast(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ForecastKind
    confidence_level: float = Field(gt=0, lt=1, description="e.g. 0.8 for an 80% interval")
    points: list[ForecastPoint] = Field(min_length=1)
    model_version: str
    method: str = Field(description="e.g. ets, auto_arima, seasonal_naive")
