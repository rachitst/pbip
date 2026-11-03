"""The fraud latency benchmark (#362): seeded inputs, fallback counting, the API's budget bounds."""

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import benchmark_fraud_latency as bench

pytestmark = pytest.mark.unit


class FakeEngine:
    """Answers at once, or (with a gate) only once the test releases it."""

    def __init__(self, gate: threading.Event | None = None, version: str = "fake-1") -> None:
        self.gate, self.version, self.calls = gate, version, 0

    def score(self, row: Any, context: Any) -> Any:
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        return SimpleNamespace(model_version=self.version)


def test_inputs_are_deterministic_per_seed() -> None:
    first, again = bench.transfer_inputs(20, seed=7), bench.transfer_inputs(20, seed=7)
    assert first == again and len(first) == 20
    assert first != bench.transfer_inputs(20, seed=8)
    assert all(i.amount_minor > 0 and i.occurred_at == bench.NOW for i in first)
    assert all(h.occurred_at < bench.NOW for i in first for h in i.history)


def test_a_fast_model_never_falls_back() -> None:
    engine = FakeEngine()
    result = bench.run_pass(engine, bench.transfer_inputs(5, seed=1), "budget", 100).report()
    assert (result["calls"], result["fallbacks"], result["fallback_rate"]) == (5, 0, 0.0)
    assert engine.calls == 5


def test_a_model_slower_than_the_budget_counts_as_a_fallback() -> None:
    gate = threading.Event()
    try:
        result = bench.run_pass(
            FakeEngine(gate), bench.transfer_inputs(1, seed=1), "back2back", 5, isolated=False
        ).report()
    finally:
        gate.set()
    assert (result["fallbacks"], result["fallback_rate"]) == (1, 1.0)


def test_a_model_that_raises_inside_counts_as_a_fallback() -> None:
    engine = FakeEngine(version="hybrid-1+rules-only")
    result = bench.run_pass(engine, bench.transfer_inputs(3, seed=1), "budget", 50).report()
    assert result["fallbacks"] == 3


def test_a_budget_the_api_refuses_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["bench", "--budget-ms", "101", "--calls", "1"])
    assert bench.main() == 2


def test_report_line_names_the_budget_rate_and_load() -> None:
    report = bench.Pass("budget", 50, [10.0, 20.0, 60.0], 1, (1.0, 0.5, 0.25), (1.0, 0.5, 0.25))
    line = bench.format_line(report.report())
    assert line.startswith("budget     budget=50 ms   n=3 p50=20.0 p95=60.0 p99=60.0 max=60.0 ms")
    assert line.endswith("fallbacks=1 (33.33%) load=1.00 0.50 0.25")
