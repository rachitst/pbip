"""TASK-110: the unusual-expense rule against the customer's own baseline (pure functions)."""

from datetime import UTC, datetime

import pytest
from pbip_api.modules.analytics.unusual import (
    MAX_ITEMS,
    MIN_EXCESS_MINOR,
    Debit,
    UnusualCategory,
    baseline_months_with_spend,
    find_unusual,
    unusual_categories,
    unusual_transactions,
)

pytestmark = pytest.mark.unit


def _d(amount: int, category: str, month: int, day: int = 10, ref: str | None = None) -> Debit:
    return Debit(amount, category, ref or category, datetime(2026, month, day, 12, tzinfo=UTC))


# Baseline July-September: groceries INR 3,000 a month, shopping INR 2,000 a month.
BASELINE = [
    *(_d(3_000_00, "groceries", m) for m in (7, 8, 9)),
    *(_d(2_000_00, "shopping", m) for m in (7, 8, 9)),
]


def test_a_category_well_above_its_monthly_average_is_unusual() -> None:
    period = [_d(2_000_00, "shopping", 10, 3), _d(12_000_00, "shopping", 10, 20)]
    assert unusual_categories(period, BASELINE) == [
        UnusualCategory("shopping", 14_000_00, 2_000_00, 2)
    ]


def test_usual_spend_is_not_flagged() -> None:
    assert unusual_categories([_d(3_500_00, "groceries", 10)], BASELINE) == []


@pytest.mark.parametrize(
    ("spent", "flagged"),
    [
        (4_500_00 - 1, False),  # just under 1.5x of INR 3,000
        (4_500_00, True),  # exactly 1.5x and INR 1,500 more
    ],
)
def test_category_ratio_boundary(spent: int, flagged: bool) -> None:
    assert bool(unusual_categories([_d(spent, "groceries", 10)], BASELINE)) is flagged


def test_the_absolute_floor_keeps_small_jumps_out() -> None:
    """INR 300 of coffee against a usual INR 100 is 3x, but not INR 1,000 more."""
    baseline = [_d(100_00, "dining", m) for m in (7, 8, 9)]
    assert unusual_categories([_d(300_00, "dining", 10)], baseline) == []
    assert unusual_categories([_d(100_00 + MIN_EXCESS_MINOR, "dining", 10)], baseline) != []


def test_a_new_category_counts_from_the_floor() -> None:
    assert unusual_categories([_d(MIN_EXCESS_MINOR - 1, "travel", 10)], BASELINE) == []
    assert unusual_categories([_d(5_000_00, "travel", 10)], BASELINE) == [
        UnusualCategory("travel", 5_000_00, 0, 1)
    ]


def test_months_without_spend_count_as_zero_in_the_average() -> None:
    """INR 3,000 in one baseline month only: the usual month is INR 1,000, not INR 3,000."""
    baseline = [_d(3_000_00, "healthcare", 8), *BASELINE]
    [item] = unusual_categories([_d(2_500_00, "healthcare", 10)], baseline)
    assert item.usual_minor == 1_000_00


def test_a_single_payment_well_above_the_category_median_is_unusual() -> None:
    period = [_d(2_000_00, "shopping", 10, 3), _d(12_000_00, "shopping", 10, 20, "Amazon TV")]
    [item] = unusual_transactions(period, BASELINE)
    assert (item.amount_minor, item.usual_minor, item.baseline_count) == (12_000_00, 2_000_00, 3)
    assert item.reference == "Amazon TV" and item.posted_at.day == 20


@pytest.mark.parametrize(("amount", "flagged"), [(6_000_00 - 1, False), (6_000_00, True)])
def test_transaction_ratio_boundary(amount: int, flagged: bool) -> None:
    assert bool(unusual_transactions([_d(amount, "shopping", 10)], BASELINE)) is flagged


def test_no_median_from_fewer_than_three_baseline_payments() -> None:
    baseline = [_d(500_00, "dining", 7), _d(500_00, "dining", 8), *BASELINE]
    assert unusual_transactions([_d(9_000_00, "dining", 10)], baseline) == []


def test_median_ignores_one_big_baseline_outlier() -> None:
    baseline = [_d(1_000_00, "dining", 7), _d(1_000_00, "dining", 8), _d(50_000_00, "dining", 9)]
    [item] = unusual_transactions([_d(4_000_00, "dining", 10)], baseline)
    assert item.usual_minor == 1_000_00


def test_items_are_ordered_and_capped() -> None:
    period = [_d(10_000_00 * (i + 1), f"cat{i}", 10) for i in range(MAX_ITEMS + 2)]
    found = find_unusual(period, BASELINE)
    assert [c.category for c in found.categories] == [f"cat{i}" for i in (6, 5, 4, 3, 2)]
    assert found.more_categories == 2 and found.transactions == []
    period = [_d(6_000_00, "shopping", 10, 1), _d(20_000_00, "shopping", 10, 2)]
    ordered = find_unusual(period, BASELINE).transactions
    assert [t.amount_minor for t in ordered] == [20_000_00, 6_000_00]  # 10x before 3x


def test_months_with_spend_counts_distinct_calendar_months() -> None:
    assert baseline_months_with_spend(BASELINE) == 3
    assert baseline_months_with_spend([_d(1, "x", 9, 1), _d(1, "x", 9, 30)]) == 1
    assert baseline_months_with_spend([]) == 0
