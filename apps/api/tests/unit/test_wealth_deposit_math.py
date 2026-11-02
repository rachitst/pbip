"""TASK-064 / ADR-0025: fixed-deposit maths is exact in integer paise.

Known values were computed independently with 50-digit ``Decimal`` arithmetic, not with the code
under test.
"""

from datetime import date, timedelta
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pbip_api.modules.wealth import deposit_math
from pbip_api.modules.wealth.service import _allocation

pytestmark = pytest.mark.unit

principals = st.integers(min_value=1, max_value=10_00_00_000_00)
rates = st.integers(min_value=0, max_value=2_000)
quarters = st.integers(min_value=1, max_value=40)
starts = st.dates(min_value=date(2024, 1, 1), max_value=date(2035, 12, 31))


@pytest.mark.parametrize(
    ("principal", "rate", "months", "amount"),
    [
        (1_00_000_00, 710, 12, 1_07_291_28),  # INR 1 lakh, 7.10%, 1 year: 4 quarters
        (1_00_000_00, 650, 6, 1_03_276_41),
        (1_50_000_00, 675, 60, 2_09_624_80),  # the tax-saver maximum over 5 years
        (25_000_00, 650, 9, 26_238_66),
    ],
)
def test_maturity_amount_compounds_quarterly_and_rounds_once(
    principal: int, rate: int, months: int, amount: int
) -> None:
    result = deposit_math.maturity(principal, rate, months, date(2026, 11, 2))
    assert result.maturity_amount_minor == amount
    assert result.interest_minor == amount - principal


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 11, 2), 12, date(2027, 11, 2)),
        (date(2026, 8, 31), 6, date(2027, 2, 28)),  # clamped to the month's last day
        (date(2028, 2, 29), 12, date(2029, 2, 28)),  # leap day
        (date(2026, 1, 31), 3, date(2026, 4, 30)),
    ],
)
def test_maturity_date_adds_calendar_months_with_month_end_clamping(
    start: date, months: int, expected: date
) -> None:
    assert deposit_math.maturity(10_000_00, 650, months, start).maturity_date == expected


@pytest.mark.parametrize(("rate", "apy"), [(710, 729), (650, 666), (675, 692), (0, 0)])
def test_apy_of_a_quarterly_rate(rate: int, apy: int) -> None:
    assert deposit_math.apy_bps(rate) == apy


def test_accrued_interest_compounds_completed_quarters_then_simple_for_the_days() -> None:
    start = date(2026, 1, 15)
    maturity = deposit_math.maturity(1_00_000_00, 710, 12, start).maturity_date
    # one completed quarter (15 Apr) and 45 days of the second
    assert deposit_math.accrued_interest(1_00_000_00, 710, start, maturity, date(2026, 5, 30)) == (
        2_665_88
    )
    assert deposit_math.accrued_interest(1_00_000_00, 710, start, maturity, start) == 0


def test_premature_interest_applies_the_penalty_and_nothing_in_the_first_week() -> None:
    start = date(2026, 1, 15)
    maturity = deposit_math.maturity(1_00_000_00, 710, 12, start).maturity_date
    on = date(2026, 5, 30)
    assert deposit_math.premature_interest(1_00_000_00, 710, 100, start, maturity, on) == 2_288_52
    week = start + timedelta(days=deposit_math.NO_INTEREST_DAYS - 1)
    assert deposit_math.premature_interest(1_00_000_00, 710, 100, start, maturity, week) == 0
    assert deposit_math.premature_interest(1_00_000_00, 50, 100, start, maturity, on) == 0  # floor


@settings(max_examples=200, deadline=None)
@given(principals, rates, quarters, starts)
def test_accrued_reaches_exactly_the_projected_interest_at_maturity(
    principal: int, rate: int, q: int, start: date
) -> None:
    result = deposit_math.maturity(principal, rate, q * 3, start)
    end = result.maturity_date
    assert deposit_math.accrued_interest(principal, rate, start, end, end) == result.interest_minor
    later = end + timedelta(days=400)  # nothing is earned after maturity
    assert (
        deposit_math.accrued_interest(principal, rate, start, end, later) == result.interest_minor
    )


@settings(max_examples=100, deadline=None)
@given(principals, rates, st.integers(min_value=1, max_value=12), starts)
def test_accrued_interest_never_falls_and_never_exceeds_the_projection(
    principal: int, rate: int, q: int, start: date
) -> None:
    result = deposit_math.maturity(principal, rate, q * 3, start)
    days = (result.maturity_date - start).days
    samples = [start + timedelta(days=d) for d in range(0, days + 1, max(days // 25, 1))]
    accrued = [
        deposit_math.accrued_interest(principal, rate, start, result.maturity_date, on)
        for on in samples
    ]
    assert all(a <= b for a, b in pairwise(accrued))
    assert all(0 <= a <= result.interest_minor for a in accrued)


@settings(max_examples=100, deadline=None)
@given(principals, rates, st.integers(min_value=0, max_value=500), quarters, starts)
def test_premature_interest_is_never_more_than_interest_at_the_contract_rate(
    principal: int, rate: int, penalty: int, q: int, start: date
) -> None:
    end = deposit_math.maturity(principal, rate, q * 3, start).maturity_date
    on = start + (end - start) / 2
    paid = deposit_math.premature_interest(principal, rate, penalty, start, end, on)
    assert 0 <= paid <= deposit_math.accrued_interest(principal, rate, start, end, on)


@pytest.mark.parametrize(
    "call",
    [
        lambda: deposit_math.maturity(0, 650, 12, date(2026, 1, 1)),
        lambda: deposit_math.maturity(10_000_00, -1, 12, date(2026, 1, 1)),
        lambda: deposit_math.maturity(10_000_00, 650, 10, date(2026, 1, 1)),  # not whole quarters
        lambda: deposit_math.maturity(10_000_00, 650, 0, date(2026, 1, 1)),
        lambda: deposit_math.apy_bps(-5),
        lambda: deposit_math.add_months(date(2026, 1, 1), -1),
        lambda: deposit_math.premature_interest(
            10_000_00, 650, -1, date(2026, 1, 1), date(2027, 1, 1), date(2026, 6, 1)
        ),
    ],
)
def test_invalid_inputs_are_refused(call: object) -> None:
    assert callable(call)
    with pytest.raises(ValueError, match="must be"):
        call()


@settings(max_examples=200, deadline=None)
@given(st.lists(st.integers(min_value=0, max_value=10**13), min_size=3, max_size=3))
def test_allocation_shares_sum_to_exactly_10000_basis_points(values: list[int]) -> None:
    slices = _allocation(dict(zip(("CASH", "DEPOSITS", "FUNDS"), values, strict=True)))
    total = sum(values)
    assert sum(s.share_bps for s in slices) == (10_000 if total else 0)
    for s in slices:
        if total:
            assert abs(s.share_bps - s.value_minor * 10_000 / total) < 1
