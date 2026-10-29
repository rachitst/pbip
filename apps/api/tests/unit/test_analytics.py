"""Unit tests for analytics categorization and service logic."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pbip_api.modules.analytics.categorization import categorize_transaction, is_debt_repayment
from pbip_api.modules.analytics.health import (
    calculate_volatility_component,
    compute_financial_health_score,
)
from pbip_api.modules.analytics.repository import HealthMetrics, _closed_months, window_start
from pbip_api.modules.analytics.schemas import (
    CategorySpendingItem,
    FinancialHealthScoreResponse,
    SpendingBreakdownResponse,
    SpendingTrendsResponse,
)
from pbip_api.modules.analytics.service import (
    is_closed_month,
    percentage_change,
    previous_closed_months,
)

pytestmark = pytest.mark.unit


def test_categorize_transaction_keywords() -> None:
    assert categorize_transaction("BigBasket Supermarket") == "groceries"
    assert categorize_transaction("Zepto provisions") == "groceries"
    assert categorize_transaction("Swiggy food order") == "dining"
    assert categorize_transaction("Cafe Coffee Day") == "dining"
    assert categorize_transaction("Uber trip Bangalore") == "transport"
    assert categorize_transaction("Petrol bunk fuel") == "transport"
    assert categorize_transaction("Electricity bill BESCOM") == "utilities"
    assert categorize_transaction("Jio Fiber broadband recharge") == "utilities"
    assert categorize_transaction("Amazon India retail") == "shopping"
    assert categorize_transaction("Myntra apparel clothing") == "shopping"
    assert categorize_transaction("Netflix subscription") == "entertainment"
    assert categorize_transaction("PVR cinema movie tickets") == "entertainment"
    assert categorize_transaction("Apollo pharmacy medicine") == "healthcare"
    assert categorize_transaction("Coursera course fee") == "education"
    assert categorize_transaction("Apartment maintenance rent") == "housing"
    assert categorize_transaction("Hotel booking Taj") == "travel"
    assert categorize_transaction(None, "TRANSFER") == "transfers"
    assert categorize_transaction(None, "FEE") == "fees"
    assert categorize_transaction("Unknown random payment") == "other"


def test_spending_breakdown_schema_validation() -> None:
    account_id = uuid4()
    item = CategorySpendingItem(
        category="groceries",
        amount_minor=5000,
        percentage=50.0,
        transaction_count=2,
    )
    resp = SpendingBreakdownResponse(
        account_id=account_id,
        currency="INR",
        period="2026-10",
        year=2026,
        month=10,
        total_spend_minor=10000,
        transaction_count=4,
        categories=[item],
    )
    assert resp.total_spend_minor == 10000
    assert resp.categories[0].category == "groceries"
    assert resp.categories[0].percentage == 50.0


def test_spending_trends_schema_validation() -> None:
    account_id = uuid4()
    resp = SpendingTrendsResponse(
        account_id=account_id,
        currency="INR",
        current_month={
            "period": "2026-10",
            "year": 2026,
            "month": 10,
            "total_spend_minor": 15000,
            "transaction_count": 5,
            "daily_burn_rate_minor": 500,
        },
        previous_month={
            "period": "2026-09",
            "year": 2026,
            "month": 9,
            "total_spend_minor": 10000,
            "transaction_count": 3,
            "daily_burn_rate_minor": 333,
        },
        mom_spend_change_percentage=50.0,
        daily_burn_rate_minor=500,
        burn_rate_change_percentage=50.15,
        top_categories_increasing=[
            {
                "category": "groceries",
                "current_amount_minor": 8000,
                "previous_amount_minor": 4000,
                "change_minor": 4000,
                "change_percentage": 100.0,
            }
        ],
        top_categories_decreasing=[],
    )
    assert resp.mom_spend_change_percentage == 50.0
    assert resp.current_month.daily_burn_rate_minor == 500


def test_percentage_change_is_undefined_without_a_baseline() -> None:
    assert percentage_change(100_000, 80_000) == 25.0
    assert percentage_change(60_000, 80_000) == -25.0
    assert percentage_change(0, 80_000) == -100.0
    assert percentage_change(50_000, 0) is None  # not "+100%": there is nothing to compare with
    assert percentage_change(0, 0) is None


def test_previous_closed_months_cross_the_year_boundary() -> None:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    assert previous_closed_months(now, 2) == [(2025, 12), (2025, 11)]
    assert is_closed_month(2025, 12, now) and not is_closed_month(2026, 1, now)
    assert not is_closed_month(2026, 2, now)


def _score(
    available: int,
    income: int,
    spend: int,
    closed_months: list[int],
    debt: int = 0,
    running: int = 0,
    months_observed: int = 1,
) -> FinancialHealthScoreResponse:
    return compute_financial_health_score(
        uuid4(),
        "INR",
        available,
        HealthMetrics(
            income_minor=income,
            spend_minor=spend,
            closed_monthly_spend_minor=closed_months,
            running_month_spend_minor=running,
            debt_repayments_minor=debt,
            months_observed=months_observed,
        ),
    )


def test_health_score_ideal_saver_scores_full_points() -> None:
    """50% savings rate, stable spend, no debt, eight months of buffer."""
    resp = _score(800_000, 200_000, 100_000, [100_000, 102_000, 98_000], months_observed=3)
    assert resp.overall_score == 100 and resp.rating == "EXCELLENT"
    assert not resp.insufficient_data and resp.window_months == 12
    assert (
        resp.components.savings_rate.score,
        resp.components.expense_volatility.score,
        resp.components.emi_to_income.score,
        resp.components.buffer_months.score,
    ) == (30, 25, 25, 20)
    assert resp.components.emi_to_income.assessment == "NO_DEBT"
    assert resp.recommendations == [
        "Your financial habits are in great shape. Keep up the steady savings rate."
    ]


def test_health_score_zero_income_burning_cash_is_zero_and_flagged() -> None:
    """DoD boundary: no income, one month of spend, nothing left: 0, not 'FAIR' by default."""
    resp = _score(0, 0, 50_000, [50_000])
    assert resp.overall_score == 0 and resp.rating == "NEEDS_ATTENTION"
    assert resp.insufficient_data is True
    assert resp.components.savings_rate.assessment == "INSUFFICIENT_DATA"
    assert resp.components.expense_volatility.assessment == "INSUFFICIENT_DATA"
    assert resp.components.emi_to_income.assessment == "INSUFFICIENT_DATA"
    assert resp.components.buffer_months.assessment == "VULNERABLE"
    assert any("No active credit inflows" in tip for tip in resp.recommendations)


def test_health_score_high_overdraft_and_debt_burden() -> None:
    """DoD boundary: overdrawn, spend above income, repayments at 70% of income."""
    resp = _score(-50_000, 100_000, 110_000, [110_000, 115_000], debt=70_000, months_observed=2)
    assert resp.components.savings_rate.assessment == "DEFICIT"
    assert resp.components.emi_to_income.score == 0
    assert resp.components.emi_to_income.assessment == "CRITICAL"
    assert resp.components.buffer_months.score == 0
    assert resp.overall_score == 25 and resp.rating == "NEEDS_ATTENTION"
    assert any("debt servicing" in tip.lower() for tip in resp.recommendations)


def test_health_score_fresh_account_is_not_rewarded_for_missing_history() -> None:
    """One deposit, no spend: savings and no-debt are real, volatility and buffer are unknown."""
    resp = _score(200_000, 200_000, 0, [], running=0)
    assert resp.overall_score == 55 and resp.rating == "FAIR"
    assert resp.insufficient_data is True
    assert resp.components.savings_rate.score == 30
    assert resp.components.expense_volatility.score == 0
    assert resp.components.emi_to_income.score == 25
    assert resp.components.buffer_months.score == 0
    assert resp.components.buffer_months.assessment == "INSUFFICIENT_DATA"


def test_health_score_tips_are_never_replaced_by_a_generic_message() -> None:
    """30 + 25 + 25 + 0 = 80 is EXCELLENT, and the empty-buffer warning must still be shown."""
    resp = _score(0, 200_000, 100_000, [100_000, 101_000], months_observed=2)
    assert resp.overall_score == 80 and resp.rating == "EXCELLENT"
    assert any("No liquid cash buffer" in tip for tip in resp.recommendations)


def test_zero_spend_months_count_as_volatility_not_as_missing_data() -> None:
    """[100k, 0, 100k] is a swing, not a stable pattern (closed months are zero-filled)."""
    resp = _score(300_000, 500_000, 200_000, [100_000, 0, 100_000], months_observed=4)
    assert resp.components.expense_volatility.assessment == "VOLATILE"
    assert resp.components.expense_volatility.metric_value == 0.7071  # CV of the three months
    assert resp.components.expense_volatility.score == 5


def test_running_month_does_not_feed_volatility_or_buffer() -> None:
    """Nine stable closed months and 3,000 spent so far this month: still OPTIMAL stability."""
    resp = _score(600_000, 1_200_000, 903_000, [100_000] * 9, running=3_000, months_observed=10)
    assert resp.components.expense_volatility.score == 25
    assert resp.components.buffer_months.metric_value == 6.0  # 600,000 / 100,000, not / 90,300


def test_mid_band_components_still_explain_their_lost_points() -> None:
    # closed months 100k/120k/140k/180k: mean 135k, CV 0.219 -> MODERATE, 21 points;
    # 500,000 available / 135,000 = 3.7 months -> ADEQUATE, 16 points
    resp = _score(
        500_000, 1_000_000, 700_000, [100_000, 120_000, 140_000, 180_000], months_observed=5
    )
    volatility = resp.components.expense_volatility
    buffer = resp.components.buffer_months
    assert (volatility.assessment, volatility.score) == ("MODERATE", 21)
    assert (buffer.assessment, buffer.score) == ("ADEQUATE", 16)
    assert any("varies by" in tip for tip in resp.recommendations)
    assert any("cash buffer covers" in tip for tip in resp.recommendations)


def test_overdraft_is_never_reported_as_insufficient_data() -> None:
    overdrawn_no_spend = _score(-5_000_000, 0, 0, [])
    assert overdrawn_no_spend.components.buffer_months.assessment == "VULNERABLE"
    assert overdrawn_no_spend.components.buffer_months.metric_value == -1.0
    overdrawn = _score(-50_000, 100_000, 110_000, [110_000, 115_000], months_observed=2)
    assert overdrawn.components.buffer_months.metric_value == round(-50_000 / 112_500, 4)
    assert any("overdrawn" in tip for tip in overdrawn.recommendations)


def test_break_even_is_worded_as_match_not_exceed() -> None:
    resp = _score(0, 100_000, 100_000, [100_000], months_observed=1)
    assert resp.components.savings_rate.assessment == "DEFICIT"
    assert any("match or exceed" in tip for tip in resp.recommendations)


def test_closed_months_run_from_the_first_entry_to_the_month_before_now() -> None:
    now = datetime(2026, 10, 30, tzinfo=UTC)
    assert _closed_months(datetime(2026, 8, 15, tzinfo=UTC), now) == [(2026, 8), (2026, 9)]
    assert _closed_months(datetime(2025, 11, 3, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)) == [
        (2025, 11),
        (2025, 12),
    ]
    assert _closed_months(now, now) == []


def test_health_score_volatility_component_scaling() -> None:
    stable, _ = calculate_volatility_component([100_000, 102_000, 99_000])
    volatile, tips = calculate_volatility_component([10_000, 250_000, 30_000])
    assert stable.score == 25 and stable.assessment == "OPTIMAL"
    assert volatile.score == 0 and volatile.assessment == "VOLATILE" and tips


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("Home loan EMI", True),
        ("Mortgage instalment March", True),
        ("Car loan installment", True),
        ("Apollo chemist pharmacy", False),  # "chemist" contains "emi"
        ("Two EMIs this month", True),
        ("Car loans repayments", True),
        ("Insurance premium", False),  # "premium" contains "emi"
        ("Remittance to family", False),
        (None, False),
    ],
)
def test_debt_repayment_detection_uses_word_boundaries(
    reference: str | None, expected: bool
) -> None:
    assert is_debt_repayment(reference) is expected


def test_window_start_covers_twelve_calendar_months() -> None:
    assert window_start(datetime(2026, 10, 29, tzinfo=UTC), 12) == datetime(2025, 11, 1, tzinfo=UTC)
    assert window_start(datetime(2026, 1, 5, tzinfo=UTC), 12) == datetime(2025, 2, 1, tzinfo=UTC)
