"""The timing benchmarks that replaced two wall-clock tests: scratch-database guard and summaries."""

import pytest

from scripts import benchmark_structuring_scoring as scoring
from scripts import benchmark_synthetic_load as load

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["pbip_test_dorm", "pbip_bench", "pbip_test"])
def test_a_scratch_database_is_accepted(name: str) -> None:
    url = f"postgresql+psycopg://127.0.0.1:5432/{name}"
    assert load.scratch_database_url(url) == url


@pytest.mark.parametrize("name", ["pbip", "postgres", "bench_pbip", ""])
def test_the_demo_or_any_other_database_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="scratch database"):
        load.scratch_database_url(f"postgresql+psycopg://127.0.0.1:5432/{name}")


@pytest.mark.parametrize("main", [load.main, scoring.main])
def test_both_benchmarks_exit_2_before_touching_a_non_scratch_database(
    main: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_migration(_url: str) -> None:
        raise AssertionError("migrated a non-scratch database")

    monkeypatch.setattr(load, "migrate", no_migration)
    monkeypatch.setattr(scoring, "migrate", no_migration)
    assert callable(main)
    assert main(["--database-url", "postgresql+psycopg://127.0.0.1:5432/pbip"]) == 2
    assert "refusing database 'pbip'" in capsys.readouterr().err


def test_the_load_summary_counts_runs_within_the_15_second_budget() -> None:
    result = load.summary([10.04, 16.0, 14.996])
    assert result == {
        "runs": 3,
        "seconds": [10.04, 16.0, 15.0],
        "median_s": 15.0,
        "max_s": 16.0,
        "within_budget": 2,
    }
