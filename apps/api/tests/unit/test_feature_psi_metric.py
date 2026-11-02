"""TASK-068: PSI results are exposed as the ``pbip_feature_psi`` gauge, labelled by feature only."""

import pytest
from pbip_api.core.metrics import record_feature_psi
from pbip_ml.monitoring.psi import PsiResult, PsiStatus
from prometheus_client import REGISTRY

pytestmark = pytest.mark.unit


def test_record_feature_psi_sets_gauge_per_feature() -> None:
    record_feature_psi(
        [
            PsiResult("amount", 0.05, PsiStatus.STABLE, 10, 12),
            PsiResult("velocity", 0.3, PsiStatus.CRITICAL, 8, 9),
        ]
    )
    assert REGISTRY.get_sample_value("pbip_feature_psi", {"feature": "amount"}) == 0.05
    assert REGISTRY.get_sample_value("pbip_feature_psi", {"feature": "velocity"}) == 0.3
