"""The latency script's statistics are correct (nearest-rank percentiles, median, max)."""

import pytest

from scripts.measure_analytics_latency import percentile, summarise

pytestmark = pytest.mark.unit


def test_nearest_rank_percentiles() -> None:
    samples = [float(v) for v in range(1, 101)]  # 1..100
    assert percentile(samples, 50) == 50.0
    assert percentile(samples, 95) == 95.0
    assert percentile(samples, 99) == 99.0
    assert percentile(samples, 100) == 100.0
    assert percentile([7.0], 95) == 7.0
    with pytest.raises(ValueError, match="no samples"):
        percentile([], 95)


def test_summary_reports_count_median_tail_and_max() -> None:
    assert summarise([10.0, 20.0, 30.0, 40.0]) == {
        "n": 4,
        "median_ms": 25.0,
        "p95_ms": 40.0,
        "p99_ms": 40.0,
        "max_ms": 40.0,
    }
