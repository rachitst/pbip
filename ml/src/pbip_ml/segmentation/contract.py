"""Customer behavioral segmentation contracts.

Defines RFM + lifestyle customer feature inputs, clustering validation reports
(Silhouette Score, Davies-Bouldin Index), and segment assignment outputs.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CustomerSegmentName(StrEnum):
    """Explainable customer behavioral personas."""

    HIGH_NET_WORTH = "HIGH_NET_WORTH"
    BUDGET_CONSCIOUS = "BUDGET_CONSCIOUS"
    FREQUENT_TRANSACTOR = "FREQUENT_TRANSACTOR"
    DISCRETIONARY_SPENDER = "DISCRETIONARY_SPENDER"
    INACTIVE_SAVER = "INACTIVE_SAVER"


class CustomerRFMFeatures(BaseModel):
    """Customer features combining Recency, Frequency, Monetary (RFM) and lifestyle ratios."""

    model_config = ConfigDict(frozen=True)

    customer_id: UUID = Field(..., description="Customer unique identifier")
    recency_days: int = Field(ge=0, description="Days elapsed since the last transaction")
    frequency: float = Field(ge=0.0, description="Average transaction count per 30-day period")
    monetary_minor: int = Field(
        ge=0, description="Average monthly expenditure in minor currency units"
    )
    savings_ratio: float = Field(
        description="Ratio of net savings to total inflows (can be negative during net deficits)"
    )
    discretionary_ratio: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of total spending on non-essential categories",
    )


class SegmentAssignment(BaseModel):
    """Assigned customer behavioral segment with confidence score and human-readable explanation."""

    model_config = ConfigDict(frozen=True)

    customer_id: UUID = Field(..., description="Customer unique identifier")
    segment: CustomerSegmentName = Field(..., description="Assigned behavioral segment")
    confidence_score: float = Field(
        ge=0.0, le=1.0, description="Clustering proximity confidence score"
    )
    assigned_at: datetime = Field(..., description="UTC timestamp of cluster assignment")
    explanation: str = Field(
        ..., description="Human-readable justification for the segment assignment"
    )


class ClusteringValidationReport(BaseModel):
    """Clustering quality evaluation metrics and cluster profile distribution."""

    model_config = ConfigDict(frozen=True)

    n_clusters: int = Field(ge=1, description="Number of clusters formed")
    n_samples: int = Field(gt=0, description="Number of customer data points clustered")
    silhouette_score: float | None = Field(
        ge=-1.0,
        le=1.0,
        description="Cohesion and separation, -1..1; None when fewer than two clusters formed",
    )
    davies_bouldin_index: float | None = Field(
        ge=0.0, description="Lower is better; None when fewer than two clusters formed"
    )
    inertia: float = Field(
        ge=0.0, description="Sum of squared distances of samples to nearest cluster center"
    )
    cluster_distribution: dict[str, int] = Field(
        description="Customer population distribution count per segment"
    )
    cluster_centroids: dict[str, dict[str, float]] = Field(
        description="Normalized centroid coordinates per segment"
    )
