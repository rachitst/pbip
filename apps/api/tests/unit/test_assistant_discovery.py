"""TASK-105: product listing and investment-suggestion shaping, without a database."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pbip_api.modules.assistant import discovery
from pbip_api.modules.assistant.discovery import EngineSuggestion, ProductCategory, RiskProfile
from pbip_api.modules.loans.catalogue import PRODUCTS as LOAN_PRODUCTS
from pbip_ml.recommendations.engine import FIXED_DEPOSIT_ID, INDEX_SIP_ID, SAVINGS_ACCOUNT_ID
from pbip_test_support import seeded_catalogue

pytestmark = pytest.mark.unit

ASSESSED = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
FD = EngineSuggestion(FIXED_DEPOSIT_ID, "Fixed deposit", ["Buffer reason.", "Health reason."])
SIP = EngineSuggestion(INDEX_SIP_ID, "Index fund SIP", ["Savings reason."])
SAVINGS = EngineSuggestion(SAVINGS_ACCOUNT_ID, "Savings account", ["Not an investment."])
FD_OPTIONS = (
    "“Fixed deposit, 6 to 9 months”: 6.50% a year (annual yield 6.66%); "
    "“Fixed deposit, 1 to 3 years”: 7.10% a year (annual yield 7.29%); "
    "“Tax-saver fixed deposit, 5 years”: 6.75% a year (annual yield 6.92%)"
)


def _profile(band: str) -> RiskProfile:
    return RiskProfile(band, "RTQ-1", ASSESSED)


def test_no_risk_profile_means_no_suggestion_and_a_pointer_to_the_questionnaire() -> None:
    text = discovery.investment_suggestions(None, [FD, SIP], "rules-v1", seeded_catalogue())
    assert text == discovery.NO_PROFILE_TEXT
    assert "Profile & security" in text and "INR" not in text


def test_a_low_band_keeps_the_deposit_and_names_the_riskier_fund_it_left_out() -> None:
    text = discovery.investment_suggestions(
        _profile("LOW"), [SIP, SAVINGS, FD], "rules-v1", seeded_catalogue()
    )
    assert text == "\n".join(
        [
            "Your risk profile is low (RTQ-1, assessed 02 Nov 2026).",
            "Suggestions from the recommendation engine (rules-v1) that fit it:",
            f"- Fixed deposit (low risk). Why: Buffer reason. Health reason. In the catalogue: "
            f"{FD_OPTIONS}.",
            "Not suggested because it is riskier than your low profile: Index fund SIP (high "
            "risk).",
            discovery.DISCLAIMER,
        ]
    )


def test_a_high_band_keeps_both_in_engine_order_with_the_illustrative_label() -> None:
    text = discovery.investment_suggestions(
        _profile("HIGH"), [SIP, FD], "rules-v1", seeded_catalogue()
    )
    lines = text.split("\n")
    assert lines[2] == (
        "- Index fund SIP (high risk). Why: Savings reason. In the catalogue: "
        "“Nifty 50 index fund (illustrative)”: market-linked, no rate shown."
    )
    assert lines[3].startswith("- Fixed deposit (low risk).")
    assert lines[-1] == f"{discovery.DISCLAIMER} {discovery.MARKET_LINKED_NOTE}"
    assert "Not suggested" not in text


def test_only_engine_output_is_suggested_never_extra_products() -> None:
    nothing = discovery.investment_suggestions(
        _profile("HIGH"), [SAVINGS], "rules-v1", seeded_catalogue()
    )
    assert "has no investment suggestion for you right now" in nothing
    assert "“" not in nothing  # no catalogue product is pushed on its own
    only_above = discovery.investment_suggestions(
        _profile("MODERATE"), [SIP], "rules-v1", seeded_catalogue()
    )
    assert "has nothing that fits it now" in only_above
    assert "riskier than your moderate profile: Index fund SIP (high risk)" in only_above


def test_an_engine_product_missing_from_the_catalogue_is_not_suggested() -> None:
    no_index = [p for p in seeded_catalogue() if p.code != "MF-INDEX"]
    text = discovery.investment_suggestions(_profile("HIGH"), [SIP], "rules-v1", no_index)
    assert "Index fund SIP" not in text
    unknown = EngineSuggestion(uuid4(), "Crypto basket", ["?"])
    assert "Crypto" not in discovery.investment_suggestions(
        _profile("HIGH"), [unknown], "rules-v1", seeded_catalogue()
    )


def test_product_list_by_category_quotes_the_catalogues() -> None:
    wealth = seeded_catalogue()
    loans = discovery.product_list(ProductCategory.LOANS, LOAN_PRODUCTS, wealth)
    assert loans.split("\n") == [
        "Loans (indicative rates, subject to eligibility):",
        "- Personal loan: from 11.50% a year, INR 25,000.00 to INR 15,00,000.00, 12 to 60 months.",
        "- Car loan: from 9.25% a year, INR 1,00,000.00 to INR 50,00,000.00, 12 to 84 months.",
        "- Home loan: from 8.50% a year, INR 5,00,000.00 to INR 5,00,00,000.00, 60 to 360 months.",
    ]
    investments = discovery.product_list(ProductCategory.INVESTMENTS, LOAN_PRODUCTS, wealth)
    assert "Loans" not in investments and "Fixed deposit," not in investments
    assert (
        "- “Sovereign gold bond (illustrative)”: moderate risk, fixed coupon 2.50% a year plus "
        "the gold price at redemption, 96 months, locked in for 60 months, not sold online."
    ) in investments
    assert (
        "- “ELSS tax-saver fund (illustrative)”: high risk, market-linked, no rate shown, from "
        "INR 500.00, locked in for 36 months, not sold online."
    ) in investments
    assert (
        "- “Nifty 50 index fund (illustrative)”: high risk, market-linked, no rate shown, from "
        "INR 500.00, can be bought online at a simulated price."
    ) in investments  # ADR-0058
    assert investments.endswith(
        "Wealth catalogue 2026-11-v1, 2026-11-v2: rates are indicative, simulated PBIP rates. "
        "Fund prices are simulated by PBIP, not market data. Products not sold online are "
        "listed for comparison only."
    )
    deposits = discovery.product_list(ProductCategory.DEPOSITS, LOAN_PRODUCTS, wealth)
    assert "- “Fixed deposit, 6 to 9 months”: low risk, 6.50% a year (annual yield 6.66%), " in (
        deposits
    )
    assert "illustrative" not in deposits
    everything = discovery.product_list(ProductCategory.ALL, LOAN_PRODUCTS, wealth)
    assert everything.count("\n- ") == len(LOAN_PRODUCTS) + len(wealth)
    assert len(everything) < 2000  # fits one stored assistant message
