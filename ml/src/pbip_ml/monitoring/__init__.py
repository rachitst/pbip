"""Model and feature monitoring: Population Stability Index drift checks (TASK-068).

Pure, deterministic computations only (numpy); exposing the results as Prometheus gauges is the
API's job (``pbip_api.core.metrics``), so this package stays free of runtime dependencies.
"""

from pbip_ml.monitoring.psi import (
    CRITICAL_THRESHOLD,
    WARNING_THRESHOLD,
    PsiResult,
    PsiStatus,
    classify,
    drift_report,
    population_stability_index,
)

__all__ = [
    "CRITICAL_THRESHOLD",
    "WARNING_THRESHOLD",
    "PsiResult",
    "PsiStatus",
    "classify",
    "drift_report",
    "population_stability_index",
]
