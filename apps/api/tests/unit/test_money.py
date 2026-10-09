from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pbip_api.core.money import Money, MoneyError

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
