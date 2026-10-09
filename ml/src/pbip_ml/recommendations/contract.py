"""Recommendation contract. Every recommendation carries human-readable reasons."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProductType(StrEnum):
    LOAN = "LOAN"
    SAVINGS = "SAVINGS"
    INVESTMENT = "INVESTMENT"
    INSURANCE = "INSURANCE"


class Recommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: UUID
    product_type: ProductType
    score: float = Field(ge=0, le=1)
    reasons: list[str] = Field(min_length=1)
    model_version: str
