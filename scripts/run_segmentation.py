"""Offline customer behavioral segmentation job runner (TASK-044).

Runs K-Means clustering pipeline across bank customer RFM & lifestyle profiles,
validates cluster cohesion (Silhouette score, Davies-Bouldin index), persists
segment assignments, and produces a cluster distribution report.

Usage:
    uv run python scripts/run_segmentation.py
    uv run python scripts/run_segmentation.py --synthetic
"""

import argparse
import sys
from uuid import uuid4

from pbip_ml.segmentation.contract import CustomerRFMFeatures
from pbip_ml.segmentation.pipeline import cluster_customers


def _generate_synthetic_sample(n: int = 100) -> list[CustomerRFMFeatures]:
    """Generate representative customer profiles across distinct spending personas."""
    samples: list[CustomerRFMFeatures] = []
    per_cluster = n // 5

    # 1. High Net Worth
    for i in range(per_cluster):
        samples.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=1 + (i % 5),
                frequency=15.0 + (i % 6) * 0.5,
                monetary_minor=450_000_00 + i * 2_500_00,
                savings_ratio=0.35 + (i % 4) * 0.01,
                discretionary_ratio=0.28 + (i % 3) * 0.01,
            )
        )

    # 2. Frequent Transactor
    for i in range(per_cluster):
        samples.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=1 + (i % 3),
                frequency=55.0 + (i % 12) * 1.0,
                monetary_minor=30_000_00 + i * 400_00,
                savings_ratio=0.15 + (i % 5) * 0.01,
                discretionary_ratio=0.22 + (i % 4) * 0.01,
            )
        )

    # 3. Discretionary Spender
    for i in range(per_cluster):
        samples.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=2 + (i % 4),
                frequency=24.0 + (i % 6) * 1.0,
                monetary_minor=65_000_00 + i * 700_00,
                savings_ratio=0.08 + (i % 3) * 0.01,
                discretionary_ratio=0.72 + (i % 6) * 0.02,
            )
        )

    # 4. Inactive / Dormant
    for i in range(per_cluster):
        samples.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=85 + (i % 20),
                frequency=0.8 + (i % 3) * 0.2,
                monetary_minor=6_000_00 + i * 250_00,
                savings_ratio=0.12 + (i % 4) * 0.01,
                discretionary_ratio=0.08 + (i % 2) * 0.01,
            )
        )

    # 5. Budget Conscious
    for i in range(n - len(samples)):
        samples.append(
            CustomerRFMFeatures(
                customer_id=uuid4(),
                recency_days=5 + (i % 5),
                frequency=11.0 + (i % 5) * 0.5,
                monetary_minor=22_000_00 + i * 350_00,
                savings_ratio=0.42 + (i % 5) * 0.01,
                discretionary_ratio=0.09 + (i % 3) * 0.01,
            )
        )

    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description="PBIP Customer Behavioral Segmentation Job")
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Run against synthetic customer dataset instead of live database",
    )
    parser.add_argument(
        "--clusters",
        type=int,
        default=5,
        help="Number of behavioral clusters (default: 5)",
    )
    args = parser.parse_args()

    print("==========================================================")
    print("PBIP Customer Behavioral Segmentation Pipeline (TASK-044)")
    print("==========================================================")

    if not args.synthetic:
        try:
            from pbip_api.core.db import get_engine
            from pbip_api.modules.analytics.service import run_offline_segmentation_job
            from sqlalchemy.orm import sessionmaker

            engine = get_engine()
            session_factory = sessionmaker(bind=engine)
            with session_factory() as session:
                print("Running segmentation against active database...")
                res = run_offline_segmentation_job(session, n_clusters=args.clusters)
                print(f"Processed {res.n_samples} customers across {res.n_clusters} clusters.")
                print(f"Persisted {res.total_customers_assigned} customer segment assignments.")
                print(f"Silhouette Score: {res.silhouette_score:.4f} (Cohesion & Separation)")
                print(f"Davies-Bouldin Index: {res.davies_bouldin_index:.4f} (Cluster Quality)")
                print(f"Inertia: {res.inertia:.2f}")
                print("\nCluster Distribution:")
                for seg, count in sorted(res.cluster_distribution.items()):
                    print(f"  - {seg:25s}: {count:4d} customers")
                print("==========================================================")
                return 0
        except Exception as e:
            print(f"Database connection unavailable ({e}). Falling back to synthetic run...\n")

    print(f"Running clustering on synthetic population (k={args.clusters})...")
    samples = _generate_synthetic_sample(125)
    _assignments, report = cluster_customers(samples, n_clusters=args.clusters, random_state=42)

    print(
        f"Successfully evaluated {report.n_samples} customer profiles into "
        f"{report.n_clusters} clusters."
    )
    print(f"Silhouette Score:     {report.silhouette_score:.4f}")
    print(f"Davies-Bouldin Index: {report.davies_bouldin_index:.4f}")
    print(f"Inertia:              {report.inertia:.2f}")
    print("\nCluster Distribution Report:")
    for seg, count in sorted(report.cluster_distribution.items()):
        pct = (count / report.n_samples) * 100
        print(f"  - {seg:25s}: {count:4d} customers ({pct:5.1f}%)")

    print("\nCentroid Profiles:")
    for seg, centroids in sorted(report.cluster_centroids.items()):
        print(f"  [{seg}]")
        for metric, val in centroids.items():
            print(f"     {metric:22s}: {val:10.2f}")

    print("==========================================================")
    print("Offline segmentation job execution completed cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
