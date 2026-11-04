"""Deterministic SIMULATED prices and unit arithmetic for investing (ADR-0058 sections 2 and 3).

This is not market data and must never be presented as such: every price is a pure function of
the instrument's seeded parameters and the bank day, so the same day gives the same price in every
environment and test, and a quote and a purchase on the same day agree. Money is integer paise and
units are integer micro-units; every division is exact (``Fraction``) or an explicit floor, so no
float is ever involved.
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Final

from pbip_api.modules.wealth.models import InvestmentInstrument

PRICE_EPOCH: Final = date(2026, 1, 1)
MICRO_PER_UNIT: Final = 1_000_000
BPS: Final = 10_000
DAYS_PER_YEAR: Final = 365
PRICE_SOURCE: Final = "SIMULATED"


@dataclass(frozen=True, slots=True)
class PriceModel:
    """The seeded parameters of one instrument (``investment_instruments`` columns)."""

    code: str
    base_minor: int  # price of one unit on PRICE_EPOCH
    drift_bps: int  # straight-line trend a year
    wobble_bps: int  # largest daily deviation from the trend


def _round_half_up(value: Fraction) -> int:
    """Whole paise, halves away from zero for the non-negative values used here."""
    return int(value + Fraction(1, 2)) if value >= 0 else -int(-value + Fraction(1, 2))


def daily_deviation_bps(code: str, day: date, wobble_bps: int) -> int:
    """A reproducible deviation in ``[-wobble_bps, +wobble_bps]`` for this instrument and day."""
    digest = hashlib.sha256(f"{code}|{day.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2 * wobble_bps + 1) - wobble_bps


def price_on(model: PriceModel, day: date) -> int:
    """Simulated price of one unit on ``day`` in paise (at least 1 paisa)."""
    days = (day - PRICE_EPOCH).days
    trend = model.base_minor * (1 + Fraction(model.drift_bps * days, BPS * DAYS_PER_YEAR))
    deviation = daily_deviation_bps(model.code, day, model.wobble_bps)
    return max(1, _round_half_up(trend * Fraction(BPS + deviation, BPS)))


def instrument_price(instrument: InvestmentInstrument, day: date) -> int:
    """``price_on`` for a seeded instrument row."""
    model = PriceModel(
        code=instrument.code,
        base_minor=instrument.price_base_minor,
        drift_bps=instrument.price_drift_bps,
        wobble_bps=instrument.price_wobble_bps,
    )
    return price_on(model, day)


def units_for_amount(amount_minor: int, price_minor: int) -> int:
    """Micro-units an amount buys, rounded DOWN: never more units than were paid for."""
    if amount_minor <= 0 or price_minor <= 0:
        raise ValueError("amount and price must be positive")
    return amount_minor * MICRO_PER_UNIT // price_minor


def value_of_units(units_micro: int, price_minor: int) -> int:
    """What units are worth in paise, rounded DOWN: the bank never pays more than their value."""
    if units_micro < 0 or price_minor <= 0:
        raise ValueError("units must be non-negative and the price positive")
    return units_micro * price_minor // MICRO_PER_UNIT


def cost_removed(cost_basis_minor: int, units_held_micro: int, units_sold_micro: int) -> int:
    """Average-cost basis taken out by a sale: all of it when everything is sold, otherwise the
    proportional share rounded down (so the remaining basis stays positive while units remain)."""
    if not 0 < units_sold_micro <= units_held_micro:
        raise ValueError("a sale must sell between one micro-unit and everything held")
    if units_sold_micro == units_held_micro:
        return cost_basis_minor
    return cost_basis_minor * units_sold_micro // units_held_micro


def gain_bps(value_minor: int, cost_minor: int) -> int | None:
    """Gain or loss as basis points of the cost (rounded half up), None when nothing is invested."""
    if cost_minor <= 0:
        return None
    return _round_half_up(Fraction((value_minor - cost_minor) * BPS, cost_minor))
