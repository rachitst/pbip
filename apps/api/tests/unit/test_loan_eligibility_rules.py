"""Loan eligibility rules 1-5 (``loan-eligibility-v2``, ADR-0056) and the UC-06 offers, as pure
functions of the customer's figures: no database."""

import pytest
from pbip_api.modules.loans import emi as emi_math
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanType
from pbip_api.modules.loans.service import (
    OFFER_TENURE_MONTHS,
    RULES_VERSION,
    EligibilityFigures,
    assess,
    figures_from,
    offers_from,
)

pytestmark = pytest.mark.unit

PERSONAL = next(p for p in PRODUCTS if p.loan_type == LoanType.PERSONAL)
HOME = next(p for p in PRODUCTS if p.loan_type == LoanType.HOME)
AMOUNT, TENURE = 1_00_000_00, 36
EMI = emi_math.emi(AMOUNT, PERSONAL.annual_rate_bps, TENURE)


def _figures(
    income: int = 1_00_000_00, existing: int = 0, active: int = 0, spend: int = 0, balance: int = 0
) -> EligibilityFigures:
    return EligibilityFigures(
        income_minor=income,
        existing_emi_minor=existing,
        active_loan_emi_minor=active,
        monthly_spend_minor=spend,
        available_balance_minor=balance or 10 * income,
    )


def test_all_five_rules_pass_and_the_reasons_cite_income_spending_and_balance() -> None:
    out = assess(PERSONAL, AMOUNT, TENURE, _figures(spend=40_000_00, balance=2_00_000_00), 0)

    assert out.status == "ELIGIBLE"
    assert out.rules_version == RULES_VERSION == "loan-eligibility-v2"
    assert out.monthly_spend_minor == 40_000_00 and out.available_balance_minor == 2_00_000_00
    assert len(out.reasons) == 3
    assert "of your income of INR 1,00,000.00, within the 50% limit" in out.reasons[0]
    assert out.reasons[1].startswith("Your spending of INR 40,000.00 a month plus this EMI")
    assert "within the 90% limit" in out.reasons[1]
    assert out.reasons[2].startswith("Your balance of INR 2,00,000.00 already covers")


def test_rule_4_spending_plus_the_new_emi_above_90_percent_refuses() -> None:
    spend = 90_000_00 - EMI + 1  # one paisa over the 90% line with the new EMI
    out = assess(PERSONAL, AMOUNT, TENURE, _figures(spend=spend), 0)

    assert out.status == "NOT_ELIGIBLE"
    assert out.spending_with_loan_ratio >= 0.90  # rounded to 4 places; the check is exact
    assert "above the 90% limit" in out.reasons[0]
    assert out.reasons[1].startswith("Over 36 months you could borrow up to")  # a smaller amount
    assert out.dti_with_loan <= 0.50  # the debt rule alone would have allowed it


def test_rule_4_at_exactly_90_percent_passes() -> None:
    out = assess(PERSONAL, AMOUNT, TENURE, _figures(spend=90_000_00 - EMI), 0)
    assert out.status == "ELIGIBLE"


def test_rule_4_counts_active_pbip_emis_on_top_of_spending() -> None:
    figures = _figures(spend=80_000_00, active=10_000_00, existing=10_000_00)
    out = assess(PERSONAL, AMOUNT, TENURE, figures, 0)
    assert out.status == "NOT_ELIGIBLE" and "above the 90% limit" in out.reasons[0]


def test_rule_5_a_balance_below_one_emi_refuses() -> None:
    out = assess(PERSONAL, AMOUNT, TENURE, _figures(balance=EMI - 1), 0)

    assert out.status == "NOT_ELIGIBLE"
    assert out.reasons[0] == (
        f"Your balance of INR {(EMI - 1) // 100:,}.{(EMI - 1) % 100:02d} does not cover one EMI "
        f"of INR {EMI // 100:,}.{EMI % 100:02d}; we look for at least the first EMI in your "
        "accounts before lending."
    )
    assert out.reasons[1].startswith("Over 36 months you could borrow up to")


def test_rule_5_a_balance_of_exactly_one_emi_passes() -> None:
    assert assess(PERSONAL, AMOUNT, TENURE, _figures(balance=EMI), 0).status == "ELIGIBLE"


def test_every_failing_rule_is_reported_and_the_old_rules_keep_their_text() -> None:
    out = assess(PERSONAL, AMOUNT, TENURE, _figures(existing=60_000_00, spend=95_000_00), 2)

    assert out.status == "NOT_ELIGIBLE"
    assert "locked 2 time(s)" in out.reasons[0]
    assert "already take 60% of your income" in out.reasons[1]
    assert "above the 90% limit" in out.reasons[2]
    assert out.max_eligible_amount_minor == 0 and out.tenor_options == []


def test_no_income_refuses_without_spending_or_balance_reasons() -> None:
    out = assess(PERSONAL, AMOUNT, TENURE, _figures(income=0, balance=1), 0)
    assert out.reasons == ["We could not find regular income in your account history."]


@pytest.mark.parametrize(
    ("spend", "balance"),
    [(0, 10_00_000_00), (70_000_00, 10_00_000_00), (0, 15_000_00), (60_000_00, 12_000_00)],
)
def test_the_suggested_maximum_always_passes_every_rule(spend: int, balance: int) -> None:
    """The budget is the tightest of rules 3-5, so each tenor's maximum is itself eligible."""
    figures = _figures(spend=spend, balance=balance)
    for option in assess(PERSONAL, PERSONAL.min_amount_minor, 12, figures, 0).tenor_options:
        at_max = assess(PERSONAL, option.max_amount_minor, option.tenure_months, figures, 0)
        assert at_max.status == "ELIGIBLE", (option, at_max.reasons)
        assert option.emi_at_max_minor <= figures.budget_minor


def test_the_budget_is_the_tightest_of_debt_spending_and_balance() -> None:
    figures = _figures(existing=10_000_00, spend=75_000_00, active=5_000_00, balance=1_00_000_00)
    assert figures.dti_room_minor == 40_000_00
    assert figures.spending_room_minor == 10_000_00
    assert figures.balance_room_minor == 1_00_000_00
    assert figures.budget_minor == 10_000_00
    assert _figures(spend=2_00_000_00).budget_minor == 0  # never negative


def test_figures_from_uses_the_same_existing_emi_formula_as_the_ledger_check() -> None:
    figures = figures_from(
        income_minor=1_00_000_00,
        emi_to_income=0.123456,
        monthly_spend_minor=1,
        active_loan_emi_minor=5_000_00,
        available_balance_minor=7,
    )
    assert figures.existing_emi_minor == round(0.123456 * 1_00_000_00) + 5_000_00
    assert figures.active_loan_emi_minor == 5_000_00


def test_offers_size_each_catalogue_loan_at_its_offer_tenor_with_a_range() -> None:
    offers = offers_from(_figures(spend=30_000_00, balance=3_00_000_00), 0)

    assert [o.product_id for o in offers.offers] == [p.id for p in PRODUCTS]
    personal = offers.offers[0]
    assert personal.status == "ELIGIBLE"
    assert personal.tenure_months == OFFER_TENURE_MONTHS[LoanType.PERSONAL] == 36
    assert personal.suggested_min_amount_minor == PERSONAL.min_amount_minor
    assert personal.suggested_min_amount_minor < personal.suggested_max_amount_minor
    assert any("Your balance of INR 3,00,000.00" in r for r in personal.reasons)
    assert any("Your spending of INR 30,000.00" in r for r in personal.reasons)


def test_an_offer_that_does_not_fit_gives_the_failing_rule_at_the_smallest_amount() -> None:
    """INR 1,00,000 income, INR 88,000 spending: the home loan's smallest EMI does not fit 90%."""
    home = offers_from(_figures(spend=88_000_00), 0).offers[2]
    assert home.product_id == HOME.id and home.status == "NOT_ELIGIBLE"
    assert home.suggested_max_amount_minor == 0 and home.emi_at_max_minor == 0
    assert "Your spending of INR 88,000.00" in home.reasons[0]
    assert "above the 90% limit" in home.reasons[0]


def test_a_low_balance_offer_names_the_balance_rule() -> None:
    home = offers_from(_figures(balance=100), 0).offers[2]
    assert home.status == "NOT_ELIGIBLE"
    assert home.reasons[0].startswith("Your balance of INR 1.00 does not cover one EMI of")


def test_a_lockout_makes_every_offer_not_eligible_with_the_lockout_reason() -> None:
    offers = offers_from(_figures(), 1).offers
    assert {o.status for o in offers} == {"NOT_ELIGIBLE"}
    assert all("locked 1 time(s)" in o.reasons[0] for o in offers)
