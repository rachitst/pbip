"""Unit tests for analytics categorization and service logic."""

from uuid import uuid4

import pytest
from pbip_api.modules.analytics.categorization import categorize_transaction
from pbip_api.modules.analytics.health import (
    calculate_volatility_component,
    compute_financial_health_score,
)
from pbip_api.modules.analytics.schemas import (
    CategorySpendingItem,
    SpendingBreakdownResponse,
    SpendingTrendsResponse,
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


def test_health_score_ideal_saver() -> None:
    """Ideal saver: 40% savings rate, stable expenses, no EMI, 8 months cash buffer."""
    resp = compute_financial_health_score(
        account_id=uuid4(),
        currency="INR",
        available_balance=800_000,  # 8000 INR
        total_credits=200_000,  # 2000 INR
        total_debits=100_000,  # 1000 INR (50% savings rate)
        monthly_debit_totals=[100_000, 102_000, 98_000],  # very stable (CV < 0.05)
        total_emi_debits=0,
    )
    assert resp.overall_score >= 90
    assert resp.rating == "EXCELLENT"
    assert resp.components.savings_rate.score == 30
    assert resp.components.expense_volatility.score == 25
    assert resp.components.emi_to_income.score == 25
    assert resp.components.buffer_months.score == 20
    assert len(resp.recommendations) >= 1


def test_health_score_zero_income_deficit() -> None:
    """Zero income and burning cash: savings deficit, zero buffer."""
    resp = compute_financial_health_score(
        account_id=uuid4(),
        currency="INR",
        available_balance=0,  # no buffer
        total_credits=0,  # no income
        total_debits=50_000,  # burning cash
        monthly_debit_totals=[50_000],
        total_emi_debits=0,
    )
    assert resp.overall_score <= 45
    assert resp.rating in ("FAIR", "NEEDS_ATTENTION")
    assert resp.components.savings_rate.score == 0
    assert resp.components.buffer_months.score == 0
    assert any("No active credit inflows" in tip for tip in resp.recommendations)


def test_health_score_high_overdraft_and_debt_burden() -> None:
    """Overdrawn account with severe EMI commitments (>60% of income)."""
    resp = compute_financial_health_score(
        account_id=uuid4(),
        currency="INR",
        available_balance=-50_000,  # overdraft
        total_credits=100_000,
        total_debits=110_000,
        monthly_debit_totals=[110_000, 115_000],
        total_emi_debits=70_000,  # 70% EMI-to-income
    )
    assert resp.components.buffer_months.score == 0
    assert resp.components.emi_to_income.score == 0
    assert resp.overall_score < 40
    assert resp.rating == "NEEDS_ATTENTION"
    assert any("debt servicing" in tip.lower() for tip in resp.recommendations)


def test_health_score_volatility_component_scaling() -> None:
    """High spending volatility drops the volatility component score."""
    stable_comp, _ = calculate_volatility_component([100_000, 102_000, 99_000])
    volatile_comp, _ = calculate_volatility_component([10_000, 250_000, 30_000])

    assert stable_comp.score == 25
    assert volatile_comp.score < 15
    assert volatile_comp.assessment == "VOLATILE"
