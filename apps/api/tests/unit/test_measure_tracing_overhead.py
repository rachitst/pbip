"""The tracing-overhead script reads the shared summary keys and reports deltas against ``off``."""

import pytest

from scripts.measure_analytics_latency import summarise
from scripts.measure_tracing_overhead import compute_deltas, format_lines

pytestmark = pytest.mark.unit


def test_deltas_use_the_shared_summary_keys() -> None:
    results = {
        "off": summarise([10.0, 10.0, 10.0, 12.0]),
        "none": summarise([10.5, 10.5, 10.5, 13.0]),
        "memory": summarise([11.0, 11.0, 11.0, 14.0]),
    }
    deltas = compute_deltas(results)
    assert deltas == {"none": {"median": 0.5, "p95": 1.0}, "memory": {"median": 1.0, "p95": 2.0}}
    lines = format_lines(results, deltas).splitlines()
    assert lines[0] == "off     n=4 median=10.0 ms p95=12.0 ms p99=12.0 ms"
    assert lines[-1] == "delta memory  median=+1.0 ms p95=+2.0 ms"
