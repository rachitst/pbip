"""The timing benchmarks that replaced three wall-clock tests: scratch-database guard and summaries."""

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


@pytest.mark.parametrize(
    ("main", "extra"),
    [(load.main, []), (scoring.main, []), (scoring.main, ["--rule", "mule"])],
    ids=["load", "structuring", "mule"],
)
def test_every_benchmark_exits_2_before_touching_a_non_scratch_database(
    main: object,
    extra: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_migration(_url: str) -> None:
        raise AssertionError("migrated a non-scratch database")

    monkeypatch.setattr(load, "migrate", no_migration)
    monkeypatch.setattr(scoring, "migrate", no_migration)
    assert callable(main)
    assert main(["--database-url", "postgresql+psycopg://127.0.0.1:5432/pbip", *extra]) == 2
    assert "refusing database 'pbip'" in capsys.readouterr().err


def test_an_unknown_rule_is_refused_before_any_database_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(scoring, "migrate", pytest.fail)
    with pytest.raises(SystemExit) as exited:
        scoring.main(
            ["--database-url", "postgresql+psycopg://127.0.0.1:5432/pbip_test", "--rule", "x"]
        )
    assert exited.value.code == 2
    assert "invalid choice: 'x'" in capsys.readouterr().err


def test_the_mule_mode_forwards_90_percent_of_what_came_in_and_passes_the_rule_minimum() -> None:
    """As the cost test: 1,000 credits of INR 100.00 (INR 1,00,000.00 >= the INR 10,000.00
    minimum), INR 90,000.00 sent on (>= the 80% ratio), so MULE_PASS_THROUGH fires."""
    mule = scoring.RULES["mule"]
    assert mule.code == "MULE_PASS_THROUGH" and mule.count_feature == "mule_inbound_count"
    assert 1_000 * mule.inbound_amount == 10_000_000
    assert mule.scored_amount(1_000) == 9_000_000
    assert mule.scored_amount(3) == 27_000


def test_the_structuring_mode_is_unchanged() -> None:
    structuring = scoring.RULES["structuring"]
    assert structuring.code == "STRUCTURING" and structuring.inbound_amount == 9_950_000
    assert structuring.count_feature == "structuring_received_count"
    assert structuring.scored_amount(1_000) == structuring.scored_amount(3) == 500_000


@pytest.mark.parametrize("mode", ["structuring", "mule"])
def test_a_run_reports_the_counted_inbound_and_whether_the_rule_fired(mode: str) -> None:
    rule = scoring.RULES[mode]
    features = {rule.count_feature: 1_000}
    fired = scoring.rule_facts(rule, features, ["NEW_PAYEE", rule.code])
    assert fired == {"rule": rule.code, "counted": 1_000, "fired": True}
    assert scoring.rule_facts(rule, features, ["NEW_PAYEE"])["fired"] is False


def test_the_load_summary_counts_runs_within_the_15_second_budget() -> None:
    result = load.summary([10.04, 16.0, 14.996])
    assert result == {
        "runs": 3,
        "seconds": [10.04, 16.0, 15.0],
        "median_s": 15.0,
        "max_s": 16.0,
        "within_budget": 2,
    }
