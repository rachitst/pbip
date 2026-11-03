"""Offline customer behavioural segmentation job (TASK-044, ADR-0012).

Clusters every customer with ledger activity on RFM and lifestyle features (K-Means), reports the
Silhouette score and Davies-Bouldin index, stores one segment row per customer in
``customer_segments`` and writes the cluster distribution report. Failures are failures: the job
exits non-zero and never silently switches to synthetic data.

Usage:
    uv run python scripts/run_segmentation.py [--report-file report.json]
    uv run python scripts/run_segmentation.py --synthetic   # pipeline check without a database
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pbip_ml.segmentation.contract import ClusteringValidationReport
from pbip_ml.segmentation.pipeline import cluster_customers
from pbip_ml.segmentation.synthetic import synthetic_customers


def print_report(report: ClusteringValidationReport, written: int | None) -> None:
    out = sys.stdout
    out.write(f"Customers clustered:   {report.n_samples} into {report.n_clusters} segments\n")
    out.write(f"Silhouette score:      {report.silhouette_score}\n")
    out.write(f"Davies-Bouldin index:  {report.davies_bouldin_index}\n")
    out.write(f"Inertia:               {report.inertia}\n")
    if written is not None:
        out.write(f"Segment rows written:  {written}\n")
    out.write("\nCluster distribution:\n")
    for segment, count in sorted(report.cluster_distribution.items()):
        out.write(f"  {segment:22s} {count:5d}  ({count / report.n_samples * 100:5.1f}%)\n")
    out.write("\nCentroids (original units):\n")
    for segment, centroid in sorted(report.cluster_centroids.items()):
        values = ", ".join(f"{name}={value}" for name, value in centroid.items())
        out.write(f"  {segment}: {values}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="PBIP customer behavioural segmentation job")
    parser.add_argument(
        "--synthetic", action="store_true", help="Cluster a synthetic population, no database"
    )
    parser.add_argument("--report-file", type=Path, default=None, help="Write the JSON report")
    args = parser.parse_args()
    now = datetime.now(UTC)

    if args.synthetic:
        _assignments, report = cluster_customers(synthetic_customers(), now=now)
        print_report(report, written=None)
        payload = report.model_dump()
    else:
        from pbip_api.core.db import session_scope
        from pbip_api.modules.analytics.segmentation_job import run_offline_segmentation_job

        with session_scope() as session:
            result = run_offline_segmentation_job(session, now)
        if result.skipped_reason is not None:
            sys.stdout.write(f"Skipped: {result.skipped_reason}\n")
        else:
            print_report(
                ClusteringValidationReport(
                    n_clusters=result.n_clusters,
                    n_samples=result.n_samples,
                    silhouette_score=result.silhouette_score,
                    davies_bouldin_index=result.davies_bouldin_index,
                    inertia=result.inertia or 0.0,
                    cluster_distribution=result.cluster_distribution,
                    cluster_centroids=result.cluster_centroids,
                ),
                written=result.total_customers_assigned,
            )
        payload = result.model_dump()

    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        sys.stdout.write(f"Wrote report to {args.report_file}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
