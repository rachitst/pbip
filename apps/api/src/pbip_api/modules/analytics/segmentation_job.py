"""Offline customer segmentation job (TASK-044, ADR-0012).

This is the only module in the API package that imports the training pipeline
(``pbip_ml.segmentation.pipeline``); the request path (router, service) imports the contract
only. Run it from ``scripts/run_segmentation.py``; never from a request.
"""

from datetime import datetime

from pbip_ml.segmentation.pipeline import MAX_CLUSTERS, cluster_customers
from sqlalchemy.orm import Session

from pbip_api.modules.analytics import repository
from pbip_api.modules.analytics.schemas import SegmentationJobResponse


def run_offline_segmentation_job(session: Session, now: datetime) -> SegmentationJobResponse:
    """Cluster every customer with ledger activity into the five personas and store the labels.

    One commit at the end. With fewer than five eligible customers nothing is clustered or
    written and the response says why, instead of failing: a brand-new bank is a normal state.
    """
    features = repository.extract_all_customers_rfm_features(session, now)
    if len(features) < MAX_CLUSTERS:
        return SegmentationJobResponse(
            n_clusters=0,
            n_samples=len(features),
            silhouette_score=None,
            davies_bouldin_index=None,
            inertia=None,
            cluster_distribution={},
            cluster_centroids={},
            total_customers_assigned=0,
            skipped_reason=(
                f"{len(features)} customer(s) with ledger activity; at least {MAX_CLUSTERS} "
                "are needed for the five personas"
            ),
        )
    assignments, report = cluster_customers(features, n_clusters=MAX_CLUSTERS, now=now)
    by_customer = {f.customer_id: f for f in features}
    for assignment in assignments:
        repository.upsert_customer_segment(session, assignment, by_customer[assignment.customer_id])
    session.commit()
    return SegmentationJobResponse(
        n_clusters=report.n_clusters,
        n_samples=report.n_samples,
        silhouette_score=report.silhouette_score,
        davies_bouldin_index=report.davies_bouldin_index,
        inertia=report.inertia,
        cluster_distribution=report.cluster_distribution,
        cluster_centroids=report.cluster_centroids,
        total_customers_assigned=len(assignments),
        skipped_reason=None,
    )
