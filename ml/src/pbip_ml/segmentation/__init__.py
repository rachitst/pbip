"""Customer behavioral segmentation package."""

from pbip_ml.segmentation.contract import (
    ClusteringValidationReport,
    CustomerRFMFeatures,
    CustomerSegmentName,
    SegmentAssignment,
)
from pbip_ml.segmentation.pipeline import cluster_customers

__all__ = [
    "ClusteringValidationReport",
    "CustomerRFMFeatures",
    "CustomerSegmentName",
    "SegmentAssignment",
    "cluster_customers",
]
