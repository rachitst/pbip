"""K-Means behavioural clustering of customers on RFM and lifestyle features (TASK-044).

Training code: imported only by the offline job, never by the API request path. Fits K-Means with
a fixed seed on standardised features, reports Silhouette and Davies-Bouldin, and names each
cluster with one of the five personas from its centroid (``_label_cluster_centroids``), so
``n_clusters`` is capped at the number of personas.
"""

from collections import Counter
from datetime import UTC, datetime
from typing import Any  # numpy array dtype/shape parameters have no narrower static type

import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import davies_bouldin_score, silhouette_score
from sklearn.preprocessing import StandardScaler

from pbip_ml.segmentation.contract import (
    ClusteringValidationReport,
    CustomerRFMFeatures,
    CustomerSegmentName,
    SegmentAssignment,
)

FEATURE_NAMES = (
    "recency_days",
    "frequency",
    "monetary_minor",
    "savings_ratio",
    "discretionary_ratio",
)
MIN_CUSTOMERS = 2
MAX_CLUSTERS = len(CustomerSegmentName)


def _label_cluster_centroids(
    centroids: np.ndarray[Any, Any], feature_names: tuple[str, ...]
) -> dict[int, CustomerSegmentName]:
    """Name clusters from their centroids, in a fixed priority order.

    Highest monetary -> HIGH_NET_WORTH; then highest recency -> INACTIVE_SAVER; then highest
    frequency -> FREQUENT_TRANSACTOR; then highest discretionary ratio -> DISCRETIONARY_SPENDER;
    the rest -> BUDGET_CONSCIOUS. Deterministic and explainable; with k < 5 the later personas
    are simply not used. Requires k <= len(CustomerSegmentName).
    """
    k = centroids.shape[0]
    assigned: dict[int, CustomerSegmentName] = {}
    available_segments = list(CustomerSegmentName)

    # Feature indices
    rec_idx = feature_names.index("recency_days")
    freq_idx = feature_names.index("frequency")
    mon_idx = feature_names.index("monetary_minor")
    disc_idx = feature_names.index("discretionary_ratio")

    # 1. Identify High Net Worth (highest monetary centroid)
    monetary_ranks = np.argsort(centroids[:, mon_idx])[::-1]
    hnw_cluster = int(monetary_ranks[0])
    assigned[hnw_cluster] = CustomerSegmentName.HIGH_NET_WORTH

    # 2. Identify Inactive / Dormant (highest recency days among remaining)
    remaining = [c for c in range(k) if c not in assigned]
    if remaining:
        recency_ranks = sorted(remaining, key=lambda c: float(centroids[c, rec_idx]), reverse=True)
        inactive_cluster = recency_ranks[0]
        assigned[inactive_cluster] = CustomerSegmentName.INACTIVE_SAVER

    # 3. Identify Frequent Transactor (highest frequency among remaining)
    remaining = [c for c in range(k) if c not in assigned]
    if remaining:
        freq_ranks = sorted(remaining, key=lambda c: float(centroids[c, freq_idx]), reverse=True)
        frequent_cluster = freq_ranks[0]
        assigned[frequent_cluster] = CustomerSegmentName.FREQUENT_TRANSACTOR

    # 4. Identify Discretionary Spender (highest discretionary ratio among remaining)
    remaining = [c for c in range(k) if c not in assigned]
    if remaining:
        disc_ranks = sorted(remaining, key=lambda c: float(centroids[c, disc_idx]), reverse=True)
        disc_cluster = disc_ranks[0]
        assigned[disc_cluster] = CustomerSegmentName.DISCRETIONARY_SPENDER

    # 5. Remaining cluster(s) -> Budget Conscious (or other unused segment)
    remaining = [c for c in range(k) if c not in assigned]
    for c in remaining:
        unused = [s for s in available_segments if s not in assigned.values()]
        if unused:
            chosen = (
                CustomerSegmentName.BUDGET_CONSCIOUS
                if CustomerSegmentName.BUDGET_CONSCIOUS in unused
                else unused[0]
            )
            assigned[c] = chosen
        else:
            # Fallback if k > len(available_segments)
            assigned[c] = CustomerSegmentName.BUDGET_CONSCIOUS

    return assigned


def _ratio(value: float, median: float) -> str:
    if median <= 0:
        return "n/a"
    return f"{value / median:.1f}x"


def _generate_explanation(
    segment: CustomerSegmentName, rfm: CustomerRFMFeatures, medians: dict[str, float]
) -> str:
    """Facts about this customer relative to the population, worded for the assigned persona.

    Only measured features appear, each compared with the population median, so the text cannot
    claim "high" for a below-median value: it states the ratio and lets it speak.
    """
    spend = _ratio(float(rfm.monetary_minor), medians["monetary_minor"])
    freq = _ratio(rfm.frequency, medians["frequency"])
    disc = f"{rfm.discretionary_ratio * 100:.0f}%"
    savings = f"{rfm.savings_ratio * 100:.0f}%"
    match segment:
        case CustomerSegmentName.HIGH_NET_WORTH:
            return (
                f"Grouped with the highest-spend cluster: monthly spend {spend} the population "
                f"median, savings ratio {savings}."
            )
        case CustomerSegmentName.FREQUENT_TRANSACTOR:
            return (
                f"Grouped with the most frequent transactors: {rfm.frequency:.1f} debits per "
                f"month ({freq} the median), monthly spend {spend} the median."
            )
        case CustomerSegmentName.DISCRETIONARY_SPENDER:
            return (
                f"Grouped with the discretionary spenders: {disc} of spend on dining, shopping, "
                f"entertainment and travel, savings ratio {savings}."
            )
        case CustomerSegmentName.INACTIVE_SAVER:
            return (
                f"Grouped with the least active customers: {rfm.recency_days} days since the last "
                f"entry, {rfm.frequency:.1f} debits per month, savings ratio {savings}."
            )
        case CustomerSegmentName.BUDGET_CONSCIOUS:
            return (
                f"Grouped with the remaining, steadier profiles: {disc} discretionary spend, "
                f"monthly spend {spend} the median, savings ratio {savings}."
            )


def cluster_customers(
    customers: list[CustomerRFMFeatures],
    n_clusters: int = 5,
    random_state: int = 42,
    now: datetime | None = None,
) -> tuple[list[SegmentAssignment], ClusteringValidationReport]:
    """Execute customer behavioral segmentation pipeline using K-Means clustering.

    Parameters:
        customers: List of customer RFM and lifestyle feature records.
        n_clusters: Target number of segments (must be <= len(customers)).
        random_state: Seed for deterministic cluster initialization.
        now: Timestamp to record for assignment (defaults to utcnow).

    Returns:
        Tuple of (list of customer segment assignments, validation report).
    """
    if not customers:
        raise ValueError("Cannot cluster an empty list of customers.")

    timestamp = now or datetime.now(UTC)
    n_samples = len(customers)

    if n_samples < MIN_CUSTOMERS:
        msg = f"At least {MIN_CUSTOMERS} customer feature records are required for clustering."
        raise ValueError(msg)
    if n_clusters != MAX_CLUSTERS:
        msg = (
            f"n_clusters must be {MAX_CLUSTERS}: the persona names are assigned by ranking the "
            "centroids, which only means something when every persona has a cluster."
        )
        raise ValueError(msg)
    if n_samples < MAX_CLUSTERS:
        msg = f"At least {MAX_CLUSTERS} customers are needed for {MAX_CLUSTERS} personas."
        raise ValueError(msg)
    k = n_clusters

    feat_matrix = np.array(
        [
            [
                float(c.recency_days),
                float(c.frequency),
                float(c.monetary_minor),
                float(c.savings_ratio),
                float(c.discretionary_ratio),
            ]
            for c in customers
        ],
        dtype=np.float64,
    )

    scaler = StandardScaler()
    scaled_matrix = scaler.fit_transform(feat_matrix)

    # Check for zero variance / identical samples edge case
    if np.all(np.isclose(scaled_matrix, scaled_matrix[0])):
        # All samples identical: assign single fallback segment cleanly
        assignments = [
            SegmentAssignment(
                customer_id=c.customer_id,
                segment=CustomerSegmentName.BUDGET_CONSCIOUS,
                confidence_score=1.0,
                assigned_at=timestamp,
                explanation="Uniform spending pattern observed.",
            )
            for c in customers
        ]
        report = ClusteringValidationReport(
            n_clusters=1,
            n_samples=n_samples,
            silhouette_score=None,
            davies_bouldin_index=None,
            inertia=0.0,
            cluster_distribution={CustomerSegmentName.BUDGET_CONSCIOUS.value: n_samples},
            cluster_centroids={
                CustomerSegmentName.BUDGET_CONSCIOUS.value: {
                    name: round(float(feat_matrix[0, i]), 4) for i, name in enumerate(FEATURE_NAMES)
                }
            },
        )
        return assignments, report

    kmeans = KMeans(n_clusters=k, random_state=random_state, n_init=10)
    labels = kmeans.fit_predict(scaled_matrix)

    # Ensure more than 1 distinct cluster was formed before calculating metrics
    unique_labels = np.unique(labels)
    sil_score: float | None = None
    db_index: float | None = None
    if 1 < len(unique_labels) < n_samples:  # both metrics need 2 <= labels <= samples - 1
        sil_score = round(float(silhouette_score(scaled_matrix, labels)), 4)
        db_index = round(float(davies_bouldin_score(scaled_matrix, labels)), 4)

    cluster_to_segment = _label_cluster_centroids(kmeans.cluster_centers_, FEATURE_NAMES)
    medians = {name: float(np.median(feat_matrix[:, i])) for i, name in enumerate(FEATURE_NAMES)}

    # Compute distance-based confidence scores
    assignments = []
    distribution_counts: Counter[str] = Counter()

    for idx, (customer, label) in enumerate(zip(customers, labels, strict=True)):
        sample_scaled = scaled_matrix[idx]
        distances = np.linalg.norm(kmeans.cluster_centers_ - sample_scaled, axis=1)
        # Softmax over negative distances, shifted by the minimum so an outlier cannot underflow
        # every term to 0 (which produced NaN confidences and aborted the job).
        exp_neg_dist = np.exp(-(distances - distances.min()))
        probs = exp_neg_dist / np.sum(exp_neg_dist)
        confidence = float(np.clip(probs[label], 0.0, 1.0))

        segment_name = cluster_to_segment[int(label)]
        distribution_counts[segment_name.value] += 1

        explanation = _generate_explanation(segment_name, customer, medians)

        assignments.append(
            SegmentAssignment(
                customer_id=customer.customer_id,
                segment=segment_name,
                confidence_score=round(confidence, 4),
                assigned_at=timestamp,
                explanation=explanation,
            )
        )

    # Convert unscaled cluster centers back to original feature scale for human interpretability
    unscaled_centers = scaler.inverse_transform(kmeans.cluster_centers_)
    centroids_dict: dict[str, dict[str, float]] = {}
    for cluster_idx, seg in cluster_to_segment.items():
        centroids_dict[seg.value] = {
            name: round(float(unscaled_centers[cluster_idx, i]), 2)
            for i, name in enumerate(FEATURE_NAMES)
        }

    report = ClusteringValidationReport(
        n_clusters=k,
        n_samples=n_samples,
        silhouette_score=sil_score,
        davies_bouldin_index=db_index,
        inertia=round(float(kmeans.inertia_), 2),
        cluster_distribution=dict(distribution_counts),
        cluster_centroids=centroids_dict,
    )

    return assignments, report
