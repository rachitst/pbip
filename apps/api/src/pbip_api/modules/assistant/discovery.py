"""Reply shaping for product discovery and investment suggestions (TASK-105). No I/O here.

The tools in :mod:`~pbip_api.modules.assistant.tools` read the real catalogues and the
customer's own risk profile and recommendation-engine output, then hand them to these pure
functions, so the wording rules can be unit-tested without a database:

* rates, amounts, tenures and lock-ins are printed exactly as the catalogue stores them;
* catalogue product names are stored text, so they are shown as delimited, capped data
  (:func:`~pbip_api.modules.assistant.sanitize.untrusted_block`), never as instructions;
* an investment suggestion is only ever a product the engine already found eligible, with the
  engine's own reasons, and only when its catalogue risk level is within the customer's band.
  Nothing here adds advice of its own.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pbip_ml.recommendations.engine import FIXED_DEPOSIT_ID, INDEX_SIP_ID

from pbip_api.core.money import format_minor
from pbip_api.modules.assistant.faq import percent
from pbip_api.modules.assistant.sanitize import untrusted_block
from pbip_api.modules.loans.catalogue import LoanProduct
from pbip_api.modules.wealth.schemas import InsuranceProductOut, WealthProductOut


class ProductCategory(StrEnum):
    ALL = "all"
    LOANS = "loans"
    DEPOSITS = "deposits"
    INVESTMENTS = "investments"  # mutual funds and the gold bond
    INSURANCE = "insurance"  # the simulated term-life and health catalogue (ADR-0028)


_RISK_ORDER = {"LOW": 0, "MODERATE": 1, "HIGH": 2}
_DEPOSIT = "FIXED_DEPOSIT"


def _months(values: Sequence[int]) -> str:
    if not values:
        return "open-ended"
    if len(values) == 1:
        return f"{values[0]} months"
    return f"{values[0]} to {values[-1]} months"


def loan_line(product: LoanProduct) -> str:
    return (
        f"- {product.name}: from {percent(product.annual_rate_bps)} a year, "
        f"{format_minor(product.min_amount_minor)} to {format_minor(product.max_amount_minor)}, "
        f"{_months(product.tenures_months)}."
    )


def _yield_text(product: WealthProductOut) -> str:
    if product.yield_basis == "GUARANTEED" and product.annual_rate_bps is not None:
        apy = f" (annual yield {percent(product.apy_bps)})" if product.apy_bps is not None else ""
        return f"{percent(product.annual_rate_bps)} a year{apy}"
    if product.yield_basis == "FIXED_COUPON" and product.annual_rate_bps is not None:
        coupon = f"fixed coupon {percent(product.annual_rate_bps)} a year"
        if product.product_type == "SOVEREIGN_GOLD_BOND":
            return coupon + " plus the gold price at redemption"
        return coupon
    return "market-linked, no rate shown"


def _amount_text(product: WealthProductOut) -> str:
    low, high = product.min_amount_minor, product.max_amount_minor
    if low is not None and high is not None:
        return f", {format_minor(low)} to {format_minor(high)}"
    if low is not None:
        return f", from {format_minor(low)}"
    return ""


def wealth_line(product: WealthProductOut) -> str:
    parts = [f"- {untrusted_block(product.name)}: {product.risk_level.lower()} risk"]
    parts.append(_yield_text(product) + _amount_text(product))
    if product.tenures_months:
        parts.append(_months(product.tenures_months))
    if product.min_lock_in_months:
        parts.append(f"locked in for {product.min_lock_in_months} months")
    if product.investable:
        parts.append("can be bought online at a simulated price")
    elif not product.bookable:
        parts.append("not sold online")
    return ", ".join(parts) + "."


def _versions(products: Sequence[WealthProductOut]) -> str:
    return ", ".join(sorted({p.catalogue_version for p in products}))


def _wealth_footer(products: Sequence[WealthProductOut]) -> str:
    if not products:
        return ""
    footer = f"Wealth catalogue {_versions(products)}: rates are indicative, simulated PBIP rates."
    if any(p.investable for p in products):
        footer += " Fund prices are simulated by PBIP, not market data."
    if any(not p.bookable and not p.investable for p in products):
        footer += " Products not sold online are listed for comparison only."
    return footer


_INSURANCE_TYPE = {"TERM_LIFE": "term life", "HEALTH": "health"}
NO_INSURANCE_TEXT = "No insurance plans are listed at the moment."


def _terms_text(years: Sequence[int]) -> str:
    if list(years) == [1]:
        return "renewed yearly"
    if len(years) == 1:
        return f"{years[0]}-year term"
    return f"{years[0]} to {years[-1]}-year terms"


def insurance_line(product: InsuranceProductOut) -> str:
    """One plan: cover, terms, entry ages and the indicative premium with its reference profile."""
    premium = product.indicative_premium
    kind = _INSURANCE_TYPE.get(product.insurance_type, product.insurance_type.lower())
    return (
        f"- {untrusted_block(product.name)}: {kind}, cover "
        f"{format_minor(product.cover_min_minor)} to {format_minor(product.cover_max_minor)}, "
        f"{_terms_text(product.policy_terms_years)}, entry ages "
        f"{product.eligibility.min_entry_age} to {product.eligibility.max_entry_age}; indicative "
        f"premium {format_minor(premium.annual_premium_minor)} a year for "
        f"{format_minor(premium.reference_cover_minor)} cover at age {premium.reference_age}."
    )


def _insurance_footer(plans: Sequence[InsuranceProductOut]) -> str:
    versions = ", ".join(sorted({p.catalogue_version for p in plans}))
    return (
        f"Insurance catalogue {versions}: simulated plans, not issued by a real insurer. "
        "Premiums are for the example customer shown, not a quote for you; nothing is "
        "underwritten."
    )


def product_list(
    category: ProductCategory,
    loans: Sequence[LoanProduct],
    wealth: Sequence[WealthProductOut],
    insurance: Sequence[InsuranceProductOut] = (),
) -> str:
    """The catalogue for one category, every figure exactly as stored."""
    deposits = [p for p in wealth if p.product_type == _DEPOSIT]
    investments = [p for p in wealth if p.product_type != _DEPOSIT]
    sections: list[str] = []
    shown: list[WealthProductOut] = []
    if category in (ProductCategory.ALL, ProductCategory.LOANS):
        sections.append("Loans (indicative rates, subject to eligibility):")
        sections.extend(loan_line(p) for p in loans)
    if category in (ProductCategory.ALL, ProductCategory.DEPOSITS):
        sections.append("Fixed deposits (book on the Wealth page):")
        sections.extend(wealth_line(p) for p in deposits)
        shown.extend(deposits)
    if category in (ProductCategory.ALL, ProductCategory.INVESTMENTS):
        sections.append("Funds and bonds:")
        sections.extend(wealth_line(p) for p in investments)
        shown.extend(investments)
    footers = [_wealth_footer(shown)]
    if category is ProductCategory.INSURANCE:
        sections.append("Insurance (simulated, see the Wealth page):")
        sections.extend(insurance_line(p) for p in insurance)
        footers.append(_insurance_footer(insurance) if insurance else NO_INSURANCE_TEXT)
    elif category is ProductCategory.ALL and insurance:
        # One line, not every plan: the whole catalogue must fit one stored message (2,000 chars).
        footers.append(
            f"Insurance (simulated): {len(insurance)} term life and health plans; ask me about "
            "insurance for their cover and indicative premiums."
        )
    return "\n".join([*sections, *(f for f in footers if f)])


# The recommendation engine's investment products and the catalogue rows that carry them.
_DEPOSIT_CODES = ("FD-SHORT", "FD-REGULAR", "FD-TAX-SAVER")
ENGINE_TO_CATALOGUE: dict[UUID, tuple[str, ...]] = {
    FIXED_DEPOSIT_ID: _DEPOSIT_CODES,
    INDEX_SIP_ID: ("MF-INDEX",),
}

NO_PROFILE_TEXT = (
    "You haven't answered the risk-tolerance questionnaire yet, so I can't suggest investments. "
    "Answer its five questions under Profile & security, then ask me again."
)
DISCLAIMER = (
    "These come from fixed rules applied to your own account figures and your questionnaire "
    "answers. They are not personalised financial advice."
)
MARKET_LINKED_NOTE = "Market-linked: returns are not guaranteed."


@dataclass(frozen=True)
class EngineSuggestion:
    """One product the recommendation engine found eligible, with its own reasons."""

    product_id: UUID
    title: str
    reasons: Sequence[str]


@dataclass(frozen=True)
class RiskProfile:
    band: str
    questionnaire_version: str | None
    assessed_at: datetime | None


def _assessed(profile: RiskProfile) -> str:
    details = [profile.questionnaire_version] if profile.questionnaire_version else []
    if profile.assessed_at is not None:
        details.append(f"assessed {profile.assessed_at:%d %b %Y}")
    return f" ({', '.join(details)})" if details else ""


def _option(product: WealthProductOut) -> str:
    return f"{untrusted_block(product.name)}: {_yield_text(product)}"


def _suggestion_line(item: EngineSuggestion, risk: str, options: list[WealthProductOut]) -> str:
    why = " ".join(item.reasons)
    listed = "; ".join(_option(p) for p in options)
    return f"- {item.title} ({risk.lower()} risk). Why: {why} In the catalogue: {listed}."


@dataclass
class _Sorted:
    fits: list[str]
    above: list[str]
    market_linked: bool = False


def _sort_by_band(
    engine: Sequence[EngineSuggestion], band: str, wealth: Sequence[WealthProductOut]
) -> _Sorted:
    by_code = {p.code: p for p in wealth}
    result = _Sorted([], [])
    for item in engine:
        options = [by_code[c] for c in ENGINE_TO_CATALOGUE.get(item.product_id, ()) if c in by_code]
        if not options:
            continue  # not an investment product, or the catalogue no longer carries it
        risk = max((p.risk_level for p in options), key=_RISK_ORDER.__getitem__)
        if _RISK_ORDER[risk] > _RISK_ORDER[band]:
            result.above.append(f"{item.title} ({risk.lower()} risk)")
            continue
        result.fits.append(_suggestion_line(item, risk, options))
        result.market_linked |= any(p.yield_basis == "MARKET_LINKED" for p in options)
    return result


def investment_suggestions(
    profile: RiskProfile | None,
    engine: Sequence[EngineSuggestion],
    model_version: str,
    wealth: Sequence[WealthProductOut],
) -> str:
    """Engine suggestions kept only when every catalogue product behind them fits the band."""
    if profile is None:
        return NO_PROFILE_TEXT
    band = profile.band.lower()
    found = _sort_by_band(engine, profile.band, wealth)
    lines = [f"Your risk profile is {band}{_assessed(profile)}."]
    if found.fits:
        lines.append(f"Suggestions from the recommendation engine ({model_version}) that fit it:")
        lines.extend(found.fits)
    elif found.above:
        lines.append(f"The recommendation engine ({model_version}) has nothing that fits it now.")
    else:
        lines.append(
            f"The recommendation engine ({model_version}) has no investment suggestion for you "
            "right now: it suggests a fixed deposit or an index fund SIP only when your own "
            "balance, savings and financial health meet its rules."
        )
    if found.above:
        lines.append(
            f"Not suggested because it is riskier than your {band} profile: "
            + "; ".join(found.above)
            + "."
        )
    lines.append(f"{DISCLAIMER} {MARKET_LINKED_NOTE}" if found.market_linked else DISCLAIMER)
    return "\n".join(lines)
