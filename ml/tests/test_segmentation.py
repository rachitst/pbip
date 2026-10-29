"""Unit tests for customer behavioral segmentation engine (TASK-044).

Verifies RFM + lifestyle feature contracts, K-Means clustering determinism,
Silhouette Score and Davies-Bouldin Index validation, and edge case handling.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pbip_ml.segmentation.contract import (
    ClusteringValidationReport,
    CustomerRFMFeatures,
    CustomerSegmentName,
)
from pbip_ml.segmentation.pipeline import cluster_customers
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _generate_synthetic_customers() -> list[CustomerRFMFeatures]:
    """Generate distinct clusters of synthetic customer RFM profiles."""
    customers: list[CustomerRFMFeatures] = []

    # Cluster 1: High Net Worth (high spend, solid savings)
    for i in range(25):
        customers.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=1 + (i % 4),
                frequency=15.0 + (i % 5) * 0.5,
                monetary_minor=500_000_00 + i * 2_000_00,  # 500k-550k INR
                savings_ratio=0.35 + (i % 3) * 0.01,
                discretionary_ratio=0.30 + (i % 3) * 0.01,
            )
        )

    # Cluster 2: Frequent Transactor (very high frequency, moderate spend)
    for i in range(25):
        customers.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=1 + (i % 2),
                frequency=60.0 + (i % 10) * 1.0,  # 60-70 transactions/month
                monetary_minor=35_000_00 + i * 500_00,
                savings_ratio=0.15 + (i % 4) * 0.01,
                discretionary_ratio=0.25 + (i % 4) * 0.01,
            )
        )

    # Cluster 3: Discretionary Spender (high dining, entertainment, shopping)
    for i in range(25):
        customers.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=2 + (i % 3),
                frequency=20.0 + (i % 5) * 1.0,
                monetary_minor=60_000_00 + i * 800_00,
                savings_ratio=0.05 + (i % 3) * 0.01,
                discretionary_ratio=0.70 + (i % 5) * 0.02,  # 70-80% discretionary
            )
        )

    # Cluster 4: Inactive / Dormant (high recency)
    for i in range(25):
        customers.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=90 + (i % 15),  # 90-105 days since last transaction
                frequency=0.5 + (i % 3) * 0.2,
                monetary_minor=5_000_00 + i * 200_00,
                savings_ratio=0.10 + (i % 3) * 0.01,
                discretionary_ratio=0.10 + (i % 2) * 0.01,
            )
        )

    # Cluster 5: Budget Conscious (frugal, low discretionary, high savings)
    for i in range(25):
        customers.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=4 + (i % 4),
                frequency=10.0 + (i % 4) * 0.5,
                monetary_minor=20_000_00 + i * 300_00,
                savings_ratio=0.40 + (i % 4) * 0.01,  # 40% savings
                discretionary_ratio=0.08 + (i % 3) * 0.01,  # only 8-10% discretionary
            )
        )

    return customers


def test_rfm_feature_contract_validation() -> None:
    """Features reject negative recency, frequency, monetary, or out-of-bounds discretionary."""
    valid_id = uuid4()

    # Valid profile passes
    feat = CustomerRFMFeatures(
        customer_id=valid_id,
        recency_days=5,
        frequency=10.5,
        monetary_minor=25_000_00,
        savings_ratio=0.20,
        discretionary_ratio=0.35,
    )
    assert feat.customer_id == valid_id

    # Negative recency
    with pytest.raises(ValidationError):
        CustomerRFMFeatures(
            customer_id=valid_id,
            recency_days=-1,
            frequency=1.0,
            monetary_minor=1000,
            savings_ratio=0.1,
            discretionary_ratio=0.2,
        )

    # Discretionary ratio > 1.0
    with pytest.raises(ValidationError):
        CustomerRFMFeatures(
            customer_id=valid_id,
            recency_days=1,
            frequency=1.0,
            monetary_minor=1000,
            savings_ratio=0.1,
            discretionary_ratio=1.5,
        )


def test_kmeans_clustering_quality_metrics() -> None:
    """Clustering pipeline produces high quality Silhouette score and Davies-Bouldin index."""
    customers = _generate_synthetic_customers()
    now = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)

    assignments, report = cluster_customers(customers, n_clusters=5, random_state=42, now=now)

    assert len(assignments) == len(customers)
    assert isinstance(report, ClusteringValidationReport)
    assert report.n_clusters == 5
    assert report.n_samples == 125

    # Verification of Silhouette Score and Davies-Bouldin quality
    assert report.silhouette_score > 0.40, f"Low silhouette score: {report.silhouette_score}"
    assert report.davies_bouldin_index < 1.5, f"High DB index: {report.davies_bouldin_index}"
    assert report.inertia > 0.0

    # Ensure all segments are populated
    for seg in CustomerSegmentName:
        assert report.cluster_distribution.get(seg.value, 0) > 0


def test_kmeans_clustering_determinism() -> None:
    """Identical random_state produces identical assignments and metrics."""
    customers = _generate_synthetic_customers()

    assign1, report1 = cluster_customers(customers, n_clusters=5, random_state=42)
    assign2, report2 = cluster_customers(customers, n_clusters=5, random_state=42)

    assert report1.silhouette_score == report2.silhouette_score
    assert report1.davies_bouldin_index == report2.davies_bouldin_index

    for a1, a2 in zip(assign1, assign2, strict=True):
        assert a1.segment == a2.segment
        assert a1.confidence_score == a2.confidence_score


def test_clustering_edge_case_empty_and_single_sample() -> None:
    """Empty or single sample raises ValueError."""
    with pytest.raises(ValueError, match="Cannot cluster an empty list"):
        cluster_customers([])

    single = [
        CustomerRFMFeatures(
            customer_id=uuid4(),
            recency_days=10,
            frequency=5.0,
            monetary_minor=10_000,
            savings_ratio=0.1,
            discretionary_ratio=0.2,
        )
    ]
    with pytest.raises(ValueError, match="At least 2 customer feature records"):
        cluster_customers(single)


def test_clustering_edge_case_identical_samples() -> None:
    """Identical customer records (zero variance) handle cleanly without divide-by-zero."""
    identical = [
        CustomerRFMFeatures(
            customer_id=uuid4(),
            recency_days=10,
            frequency=5.0,
            monetary_minor=10_000,
            savings_ratio=0.1,
            discretionary_ratio=0.2,
        )
        for _ in range(10)
    ]
    assignments, report = cluster_customers(identical, n_clusters=3, random_state=42)

    assert len(assignments) == 10
    assert report.n_samples == 10
    assert all(a.confidence_score == 1.0 for a in assignments)
    assert report.silhouette_score == 0.0
