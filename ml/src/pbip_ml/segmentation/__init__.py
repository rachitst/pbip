"""Customer behavioural segmentation: the contract (inference-side types) and the training pipeline.

Only the contract is imported here. The pipeline pulls in scikit-learn and must be imported
explicitly (``pbip_ml.segmentation.pipeline``) by the offline job, never by the API request path
(AGENTS.md section 2; ``apps/api/tests/unit/test_training_code_stays_off_the_request_path.py``).
"""

from pbip_ml.segmentation.contract import (
    ClusteringValidationReport,
    CustomerRFMFeatures,
    CustomerSegmentName,
    SegmentAssignment,
)

__all__ = [
    "ClusteringValidationReport",
    "CustomerRFMFeatures",
    "CustomerSegmentName",
    "SegmentAssignment",
]
