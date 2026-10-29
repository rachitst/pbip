"""Unit tests for analytics categorization and service logic."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pbip_api.modules.analytics.categorization import categorize_transaction
from pbip_api.modules.analytics.schemas import (
    CategorySpendingItem,
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
