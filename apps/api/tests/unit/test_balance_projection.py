"""TASK-043: the composite end-of-month projection (model forecast + known obligations)."""

from datetime import date, timedelta
from uuid import uuid4

import pytest
from pbip_api.modules.analytics.projection import (
    LOW_BALANCE_BUFFER_MINOR,
    ModelDay,
    Obligation,
    ObligationKind,
    discretionary_history,
    is_low,
    low_balance_threshold,
    month_end,
    project,
)

pytestmark = pytest.mark.unit

DAY0 = date(2026, 11, 2)


def _flat(value: float, days: int, spread: float = 0.0) -> list[ModelDay]:
    return [
        ModelDay(
            median=value,
            lower_80=value - spread,
            upper_80=value + spread,
            lower_95=value - 2 * spread,
            upper_95=value + 2 * spread,
        )
        for _ in range(days)
    ]


def _obligation(
    day: date, amount: int, kind: ObligationKind = ObligationKind.LOAN_EMI
) -> Obligation:
    return Obligation(
        date=day,
        amount_minor=amount,
        kind=kind,
        scheduled_transfer_id=uuid4(),
        loan_id=None,
        reference=None,
    )


def test_scheduled_debits_are_added_back_from_their_day_on() -> None:
    # 100 -> an EMI of 30 on day 2 -> 70; a card spend of 5 on day 3 stays in the series.
    history = [100, 100, 70, 65]
    adjusted, added = discretionary_history(history, DAY0, {DAY0 + timedelta(days=2): 30})
    assert adjusted == [100, 100, 100, 95]
    assert added == 30


def test_debits_outside_the_history_are_ignored() -> None:
    adjusted, added = discretionary_history([10, 10], DAY0, {DAY0 - timedelta(days=1): 99})
    assert (adjusted, added) == ([10, 10], 0)


def test_projection_subtracts_history_add_back_and_cumulative_obligations() -> None:
    # The model saw the balance as if the 30 EMI had not been paid: 130 instead of 100.
    days = project(
        _flat(130.4, 5, spread=10),
        DAY0,
        scheduled_paid_in_history=30,
        obligations=[
            _obligation(DAY0 + timedelta(days=1), 20),
            _obligation(DAY0 + timedelta(days=3), 15, ObligationKind.SCHEDULED_TRANSFER),
            _obligation(DAY0 + timedelta(days=3), 5),
        ],
    )
    assert [d.date for d in days] == [DAY0 + timedelta(days=i) for i in range(5)]
    assert [d.predicted_minor for d in days] == [100, 80, 80, 60, 60]
    assert [d.obligations_minor for d in days] == [0, 20, 0, 20, 0]
    # Obligations are known: the interval moves with them and keeps its width.
    assert [(d.lower_80_minor, d.upper_80_minor) for d in days][3] == (50, 70)
    assert [(d.lower_95_minor, d.upper_95_minor) for d in days][3] == (40, 80)


def test_without_obligations_the_projection_is_the_model_forecast() -> None:
    days = project(_flat(5_000_00.6, 3, spread=100), DAY0, 0, [])
    assert all(d.predicted_minor == 5_000_01 and d.obligations_minor == 0 for d in days)


def test_an_obligation_after_the_forecast_does_not_count() -> None:
    days = project(_flat(100, 2), DAY0, 0, [_obligation(DAY0 + timedelta(days=2), 50)])
    assert [d.predicted_minor for d in days] == [100, 100]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 11, 2), date(2026, 11, 30)),
        (date(2026, 12, 31), date(2026, 12, 31)),
        (date(2028, 2, 10), date(2028, 2, 29)),
    ],
)
def test_month_end(day: date, expected: date) -> None:
    assert month_end(day) == expected


@pytest.mark.parametrize(
    ("predicted", "overdraft", "low"),
    [
        (LOW_BALANCE_BUFFER_MINOR + 1, 0, False),
        (LOW_BALANCE_BUFFER_MINOR, 0, True),
        (0, 0, True),
        (-5_00, 0, True),
        (0, 2_000_00, False),
        (LOW_BALANCE_BUFFER_MINOR - 2_000_00, 2_000_00, True),
    ],
)
def test_threshold_is_the_buffer_above_zero_or_the_overdraft_limit(
    predicted: int, overdraft: int, low: bool
) -> None:
    (day,) = project(_flat(predicted, 1), DAY0, 0, [])
    assert low_balance_threshold(overdraft) == LOW_BALANCE_BUFFER_MINOR - overdraft
    assert is_low(day, low_balance_threshold(overdraft)) is low
