from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pbip_api.core.money import Money, MoneyError, format_minor, group_indian

pytestmark = pytest.mark.unit

minor = st.integers(min_value=-(10**12), max_value=10**12)


def test_from_decimal_converts_to_minor_units() -> None:
    assert Money.from_decimal("12.34").minor_units == 1234
    assert Money.from_decimal(Decimal("0.05")).minor_units == 5


@pytest.mark.parametrize("bad", ["1.001", "abc", "NaN", "Infinity", 1.5])
def test_invalid_amounts_are_rejected(bad: object) -> None:
    with pytest.raises(MoneyError):
        Money.from_decimal(bad)  # type: ignore[arg-type]


def test_currency_mismatch_is_rejected() -> None:
    with pytest.raises(MoneyError, match="currency mismatch"):
        _ = Money(100, "INR") + Money(100, "GBP")


def test_bool_and_float_minor_units_are_rejected() -> None:
    with pytest.raises(MoneyError):
        Money(True)
    with pytest.raises(MoneyError):
        Money(1.5)  # type: ignore[arg-type]


@given(minor, minor)
def test_addition_and_subtraction_are_inverse(a: int, b: int) -> None:
    assert (Money(a) + Money(b)) - Money(b) == Money(a)


@given(minor)
def test_decimal_round_trip_is_lossless(a: int) -> None:
    assert Money.from_decimal(Money(a).to_decimal()) == Money(a)


@given(minor, minor)
def test_ordering_matches_integers(a: int, b: int) -> None:
    assert (Money(a) < Money(b)) == (a < b)
    assert (Money(a) >= Money(b)) == (a >= b)


@pytest.mark.parametrize(
    ("minor", "text"),
    [(0, "INR 0.00"), (5, "INR 0.05"), (123_45, "INR 123.45"), (1_000_00, "INR 1,000.00"),
     (1_00_000_00, "INR 1,00,000.00"), (12_34_56_789_00, "INR 12,34,56,789.00"),
     (-99_99_999_99, "-INR 99,99,999.99")],
)  # fmt: skip
def test_format_minor_uses_indian_grouping_like_the_web(minor: int, text: str) -> None:
    assert format_minor(minor) == text  # same output as formatMinor in apps/web/src/lib/money.ts


@given(st.integers(min_value=0, max_value=10**15))
def test_indian_grouping_keeps_every_digit(whole: int) -> None:
    grouped = group_indian(whole)
    assert grouped.replace(",", "") == str(whole)
    groups = grouped.split(",")
    assert len(groups[-1]) <= 3 and all(len(g) == 2 for g in groups[1:-1])
