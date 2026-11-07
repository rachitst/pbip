"""ADR-0058: the simulated price source and the unit arithmetic are exact and deterministic."""

from datetime import date, timedelta
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pbip_api.modules.wealth import pricing
from pbip_api.modules.wealth.pricing import MICRO_PER_UNIT, PRICE_EPOCH, PriceModel

pytestmark = pytest.mark.unit

days = st.integers(min_value=-3_650, max_value=3_650)
bases = st.integers(min_value=1_00, max_value=1_00_000_00)
drifts = st.integers(min_value=-5_000, max_value=5_000)
wobbles = st.integers(min_value=0, max_value=2_000)


def test_without_deviation_the_price_is_the_straight_line_trend() -> None:
    flat = PriceModel(code="X", base_minor=1_000_00, drift_bps=3_650, wobble_bps=0)
    assert pricing.price_on(flat, PRICE_EPOCH) == 1_000_00
    # 36.50% a year for 100 days = +10% exactly
    assert pricing.price_on(flat, PRICE_EPOCH + timedelta(days=100)) == 1_100_00
    assert pricing.price_on(flat, PRICE_EPOCH - timedelta(days=100)) == 900_00


def test_the_price_never_falls_below_one_paisa() -> None:
    falling = PriceModel(code="X", base_minor=1_00, drift_bps=-5_000, wobble_bps=0)
    assert pricing.price_on(falling, PRICE_EPOCH + timedelta(days=3_650)) == 1


@given(days, bases, drifts, wobbles)
def test_the_price_is_deterministic_and_within_the_daily_deviation(
    offset: int, base: int, drift: int, wobble: int
) -> None:
    model = PriceModel(code="MF-TEST", base_minor=base, drift_bps=drift, wobble_bps=wobble)
    day = PRICE_EPOCH + timedelta(days=offset)
    price = pricing.price_on(model, day)
    assert price == pricing.price_on(model, day)  # same day, same price
    trend = base * (1 + Fraction(drift * offset, 10_000 * 365))
    if trend > 1:
        low = trend * Fraction(10_000 - wobble, 10_000)
        high = trend * Fraction(10_000 + wobble, 10_000)
        assert low - Fraction(1, 2) <= price <= high + Fraction(1, 2)


def test_the_deviation_depends_on_the_instrument_and_the_day() -> None:
    first = date(2026, 11, 4)
    values = {
        pricing.daily_deviation_bps("STK-A", first + timedelta(days=n), 300) for n in range(30)
    }
    assert len(values) > 10 and all(-300 <= v <= 300 for v in values)
    assert pricing.daily_deviation_bps("STK-A", first, 0) == 0
    others = [pricing.daily_deviation_bps(c, first, 300) for c in ("STK-A", "STK-B", "STK-C")]
    assert len(set(others)) > 1


def test_units_are_rounded_down_and_value_is_rounded_down() -> None:
    assert pricing.units_for_amount(1_000_00, 300_00) == 3_333_333  # 3.333333 units
    assert pricing.value_of_units(3_333_333, 300_00) == 99_999  # never more than paid
    assert pricing.value_of_units(2 * MICRO_PER_UNIT, 845_50) == 1_691_00
    assert pricing.value_of_units(0, 845_50) == 0
    with pytest.raises(ValueError, match="must be positive"):
        pricing.units_for_amount(0, 100)
    with pytest.raises(ValueError, match="non-negative"):
        pricing.value_of_units(-1, 100)


@given(
    st.integers(min_value=1, max_value=10**12),
    st.integers(min_value=2, max_value=10**12),
    st.data(),
)
def test_average_cost_leaves_a_positive_basis_while_units_remain(
    cost: int, held: int, data: st.DataObject
) -> None:
    sold = data.draw(st.integers(min_value=1, max_value=held - 1))
    removed = pricing.cost_removed(cost, held, sold)
    assert 0 <= removed < cost
    assert pricing.cost_removed(cost, held, held) == cost


def test_cost_removed_refuses_impossible_sales() -> None:
    with pytest.raises(ValueError, match="between one micro-unit and everything held"):
        pricing.cost_removed(100, 5, 6)
    with pytest.raises(ValueError, match="between one micro-unit and everything held"):
        pricing.cost_removed(100, 5, 0)


def test_gain_in_basis_points() -> None:
    assert pricing.gain_bps(11_000, 10_000) == 1_000
    assert pricing.gain_bps(9_500, 10_000) == -500
    assert pricing.gain_bps(10_000, 0) is None
