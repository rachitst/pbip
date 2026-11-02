"""TASK-068: PSI drift monitor - formula, binning, thresholds, windows and edge cases."""

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pbip_ml.monitoring.psi import (
    EPSILON,
    PsiStatus,
    bin_proportions,
    classify,
    daily_velocities,
    drift_report,
    in_window,
    population_stability_index,
    psi_from_proportions,
    quantile_edges,
)

pytestmark = pytest.mark.unit

END = datetime(2026, 11, 1, tzinfo=UTC)


def test_psi_formula_matches_hand_computation() -> None:
    expected = [0.25, 0.25, 0.25, 0.25]
    actual = [0.1, 0.2, 0.3, 0.4]
    by_hand = (
        (0.1 - 0.25) * math.log(0.1 / 0.25)
        + (0.2 - 0.25) * math.log(0.2 / 0.25)
        + (0.3 - 0.25) * math.log(0.3 / 0.25)
        + (0.4 - 0.25) * math.log(0.4 / 0.25)
    )
    psi = psi_from_proportions(expected, actual)
    assert psi == pytest.approx(by_hand, abs=1e-12)
    assert psi == pytest.approx(0.228218, abs=1e-6)
    assert classify(psi) is PsiStatus.WARNING


def test_identical_distributions_have_zero_psi() -> None:
    values = [float(v) for v in range(1, 101)]
    assert population_stability_index(values, values) == 0.0
    assert psi_from_proportions([0.5, 0.5], [0.5, 0.5]) == 0.0


def test_empty_bin_uses_epsilon_floor_without_renormalising() -> None:
    psi = psi_from_proportions([0.5, 0.5], [1.0, 0.0])
    by_hand = 0.5 * math.log(1.0 / 0.5) + (EPSILON - 0.5) * math.log(EPSILON / 0.5)
    assert psi == pytest.approx(by_hand, abs=1e-12)
    assert classify(psi) is PsiStatus.CRITICAL


def test_bins_are_right_closed_with_open_outer_bins() -> None:
    edges = [10.0, 20.0, 30.0]
    # -5 and 10 -> bin 0; 10.5 and 20 -> bin 1; 25 and 30 -> bin 2; 31 and 1000 -> bin 3
    shares = bin_proportions([-5, 10, 10.5, 20, 30, 31, 1000, 25], edges)
    assert shares == [2 / 8, 2 / 8, 2 / 8, 2 / 8]


def test_quantile_edges_are_reference_quantiles_and_deduplicated() -> None:
    assert quantile_edges([1, 2, 3, 4, 5, 6, 7, 8, 9], bins=4) == [3.0, 5.0, 7.0]
    assert quantile_edges([1, 1, 1, 1, 2], bins=4) == [1.0]


def test_known_shift_between_reference_and_current() -> None:
    reference = [float(v) for v in range(1, 9)]  # quartile cuts 2.75, 4.5, 6.25 -> 2 per bin
    current = [1.0, 1.0, 1.0, 1.0, 1.0, 3.0, 5.0, 8.0]  # shares 5/8, 1/8, 1/8, 1/8
    by_hand = (5 / 8 - 1 / 4) * math.log((5 / 8) / (1 / 4)) + 3 * (
        (1 / 8 - 1 / 4) * math.log((1 / 8) / (1 / 4))
    )
    assert population_stability_index(reference, current, bins=4) == pytest.approx(
        by_hand, abs=1e-12
    )


@pytest.mark.parametrize(
    ("psi", "status"),
    [
        (0.0, PsiStatus.STABLE),
        (0.0999999, PsiStatus.STABLE),
        (0.1, PsiStatus.WARNING),
        (0.25, PsiStatus.WARNING),
        (0.2500001, PsiStatus.CRITICAL),
        (3.0, PsiStatus.CRITICAL),
    ],
)
def test_threshold_boundaries(psi: float, status: PsiStatus) -> None:
    assert classify(psi) is status


@pytest.mark.parametrize("bad", [-0.01, math.nan])
def test_classify_rejects_invalid_psi(bad: float) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        classify(bad)


def test_window_is_half_open_and_timezone_aware() -> None:
    start = END - timedelta(days=30)
    stamps = [start - timedelta(seconds=1), start, END - timedelta(microseconds=1), END]
    assert in_window(stamps, start, END) == [1, 2]
    ist = timezone(timedelta(hours=5, minutes=30))
    assert in_window([END.astimezone(ist)], start, END) == []
    naive = datetime(2026, 10, 15)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="timezone-aware"):
        in_window([naive], start, END)


def test_daily_velocities_count_per_entity_per_utc_day() -> None:
    day = datetime(2026, 10, 20, 23, 30, tzinfo=UTC)
    keys = ["a", "a", "a", "b"]
    stamps = [day, day + timedelta(minutes=10), day + timedelta(hours=1), day]  # 3rd: next day
    assert sorted(daily_velocities(keys, stamps, [0, 1, 2, 3])) == [1.0, 1.0, 2.0]


def _series(days: range, per_day: int, amount: int) -> tuple[list[int], list[str], list[datetime]]:
    amounts: list[int] = []
    keys: list[str] = []
    stamps: list[datetime] = []
    for d in days:
        for n in range(per_day):
            amounts.append(amount + 100 * n)
            keys.append(f"acct-{n % 3}")
            stamps.append(END - timedelta(days=d, hours=1))
    return amounts, keys, stamps


def test_drift_report_stable_when_windows_match() -> None:
    amounts, keys, stamps = _series(range(60), per_day=6, amount=1_000)
    report = drift_report(amounts, keys, stamps, window_end=END)
    assert [(r.feature, r.psi, r.status) for r in report] == [
        ("amount", 0.0, PsiStatus.STABLE),
        ("velocity", 0.0, PsiStatus.STABLE),
    ]
    assert report[0].reference_size == report[0].current_size == 180


def test_drift_report_flags_shifted_current_window() -> None:
    ref = _series(range(30, 60), per_day=3, amount=1_000)
    cur = _series(range(30), per_day=9, amount=50_000)
    report = drift_report(ref[0] + cur[0], ref[1] + cur[1], ref[2] + cur[2], window_end=END)
    assert [r.status for r in report] == [PsiStatus.CRITICAL, PsiStatus.CRITICAL]


def test_data_outside_both_windows_is_ignored() -> None:
    amounts, keys, stamps = _series(range(60), per_day=6, amount=1_000)
    old = _series(range(60, 90), per_day=20, amount=999_999)
    report = drift_report(amounts + old[0], keys + old[1], stamps + old[2], window_end=END)
    assert [r.psi for r in report] == [0.0, 0.0]


def test_empty_and_mismatched_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="need transactions"):
        drift_report([], [], [], window_end=END)
    only_current = _series(range(5), per_day=2, amount=1_000)
    with pytest.raises(ValueError, match="need transactions"):
        drift_report(*only_current, window_end=END)
    with pytest.raises(ValueError, match="equal length"):
        drift_report([1], [], [END], window_end=END)
    with pytest.raises(ValueError, match="window_days"):
        drift_report([1], ["a"], [END], window_end=END, window_days=0)
    with pytest.raises(ValueError, match="empty"):
        quantile_edges([])
    with pytest.raises(ValueError, match="at least 2"):
        quantile_edges([1.0], bins=1)
    with pytest.raises(ValueError, match="empty window"):
        bin_proportions([], [1.0])
    with pytest.raises(ValueError, match="equal length"):
        psi_from_proportions([0.5, 0.5], [1.0])
