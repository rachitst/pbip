"""#274 / ADR-0052: the pure structuring-pattern search the deposit monitor and the eval share."""

from datetime import UTC, datetime, timedelta

import pytest
from pbip_api.modules.fraud.structuring import Credit, StructuringBand, find_patterns

pytestmark = pytest.mark.unit

BAND = StructuringBand(10_000_000, 10, timedelta(hours=72), 3)  # INR 1,00,000; 90%; 72 h; 3
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
JUST_UNDER = 9_950_000


def credits(*hours: float, amount: int = JUST_UNDER, account: str = "A") -> list[Credit]:
    return [
        Credit(f"{account}{i}", account, T0 + timedelta(hours=h), amount)
        for i, h in enumerate(hours)
    ]


@pytest.mark.parametrize(
    ("amount", "flagged"),
    [(9_000_000, True), (8_999_999, False), (9_999_999, True), (10_000_000, False)],
)
def test_band_edges_are_exact_integers(amount: int, flagged: bool) -> None:
    assert bool(find_patterns(credits(0, 1, 2, amount=amount), BAND)) is flagged


def test_window_is_inclusive_and_one_second_more_is_out() -> None:
    assert len(find_patterns(credits(0, 30, 72), BAND)) == 1
    late = [*credits(0, 30), Credit("late", "A", T0 + timedelta(hours=72, seconds=1), JUST_UNDER)]
    assert find_patterns(late, BAND) == []


def test_fewer_than_min_count_is_not_a_pattern_and_min_count_is_a_setting() -> None:
    assert find_patterns(credits(0, 1), BAND) == []
    two = StructuringBand(10_000_000, 10, timedelta(hours=72), 2)
    assert len(find_patterns(credits(0, 1), two)) == 1


def test_accounts_are_separate_and_out_of_band_credits_are_ignored() -> None:
    mixed = credits(0, 1, account="A") + credits(2, account="B") + credits(3, amount=5_000_000)
    assert find_patterns(mixed, BAND) == []


def test_one_pattern_holds_every_credit_in_its_window_and_the_search_moves_on() -> None:
    found = find_patterns(credits(0, 10, 20, 30, 200, 210, 220), BAND)
    assert [[c.transaction_id for c in p.credits] for p in found] == [
        ["A0", "A1", "A2", "A3"],
        ["A4", "A5", "A6"],
    ]
    assert found[0].total_minor == 4 * JUST_UNDER and found[0].last.transaction_id == "A3"


def test_covered_credits_never_count_again() -> None:
    rows = credits(0, 1, 2, 3)
    assert find_patterns(rows, BAND, frozenset({"A0", "A1"})) == []
    assert len(find_patterns(rows, BAND, frozenset({"A0"}))) == 1


def test_order_of_the_input_does_not_matter() -> None:
    rows = credits(0, 10, 20, 100, 110, 120)
    assert find_patterns(list(reversed(rows)), BAND) == find_patterns(rows, BAND)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"threshold_minor": 0},
        {"band_percent": 0},
        {"band_percent": 100},
        {"window": timedelta(0)},
        {"min_count": 1},
    ],
)
def test_invalid_bands_are_refused(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {
        "threshold_minor": 10_000_000, "band_percent": 10, "window": timedelta(hours=1),
        "min_count": 3,
    }  # fmt: skip
    with pytest.raises(ValueError, match="must be"):
        StructuringBand(**{**base, **kwargs})  # type: ignore[arg-type]  # deliberately mixed
