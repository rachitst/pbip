"""Approved, read-only tool registry for the AI assistant (TASK-057, Blueprint section 10).

The registry is the ONLY path from model output to customer data:

* every tool takes a strict, bounded argument model (``extra="forbid"``, enums, numeric limits);
* no argument names a customer, a user or an account id. Accounts are picked by position
  (``account=1`` is the first account) among the AUTHENTICATED customer's own accounts, so the
  model has nothing it could point at another customer;
* the customer and user ids come from :class:`ToolContext`, which the service builds from the
  access token - never from the model or the request body;
* tools call the same service/policy functions as the REST API (ownership re-checked there);
* tools only read. The service runs them inside a ``READ ONLY`` database transaction, so even a
  buggy tool cannot change state (PostgreSQL rejects the write).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pbip_ml.recommendations import PRODUCT_CATALOGUE
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from pbip_api.core.errors import DomainError
from pbip_api.core.money import format_minor
from pbip_api.core.pagination import PageDirection
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.assistant import discovery, faq
from pbip_api.modules.assistant.discovery import ProductCategory
from pbip_api.modules.assistant.faq import FaqTopic
from pbip_api.modules.assistant.sanitize import untrusted_block
from pbip_api.modules.customers import service as customers_service
from pbip_api.modules.loans import service as loans_service
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanProduct, LoanType
from pbip_api.modules.recommendations import service as recommendations_service
from pbip_api.modules.wealth import service as wealth_service

MAX_ACCOUNTS = 10
MAX_TRANSACTIONS = 20
_PAISE_PER_RUPEE = 100


class ToolName(StrEnum):
    ACCOUNT_SUMMARY = "get_account_summary"
    RECENT_TRANSACTIONS = "get_recent_transactions"
    SPEND = "get_spend"
    BALANCE_FORECAST = "get_balance_forecast"
    LOAN_AFFORDABILITY = "get_loan_affordability"
    UNUSUAL_EXPENSES = "get_unusual_expenses"
    FAQ_ANSWER = "get_faq_answer"
    LIST_PRODUCTS = "list_products"
    INVESTMENT_SUGGESTIONS = "get_investment_suggestions"


class SpendPeriod(StrEnum):
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"


class SpendCategory(StrEnum):
    """The categories the analytics categoriser produces (``analytics/categorization.py``)."""

    GROCERIES = "groceries"
    UTILITIES = "utilities"
    DINING = "dining"
    TRANSPORT = "transport"
    SHOPPING = "shopping"
    ENTERTAINMENT = "entertainment"
    TRAVEL = "travel"
    HEALTHCARE = "healthcare"
    EDUCATION = "education"
    HOUSING = "housing"
    TRANSFERS = "transfers"
    OTHER = "other"


class _Args(BaseModel):
    """Unknown keys are rejected: a model that adds ``customer_id`` gets a validation error."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountSummaryArgs(_Args):
    """No arguments: always the authenticated customer's own accounts."""


class AccountArgs(_Args):
    account: int = Field(
        default=1, ge=1, le=MAX_ACCOUNTS, description="Position of one of YOUR accounts; 1 = first"
    )


class RecentTransactionsArgs(AccountArgs):
    limit: int = Field(default=5, ge=1, le=MAX_TRANSACTIONS)


class SpendArgs(AccountArgs):
    period: SpendPeriod = SpendPeriod.THIS_MONTH
    category: SpendCategory | None = None


class ForecastArgs(AccountArgs):
    horizon_days: Literal[7, 14, 30] = 30


class UnusualExpensesArgs(AccountArgs):
    period: SpendPeriod = SpendPeriod.THIS_MONTH


class FaqArgs(_Args):
    topic: FaqTopic


class ProductsArgs(_Args):
    category: ProductCategory = ProductCategory.ALL


class InvestmentSuggestionsArgs(_Args):
    """No arguments: the risk profile and engine output are the authenticated customer's own."""


# A sensible tenure per product when the customer names none (all offered by the catalogue).
DEFAULT_TENURE_MONTHS = {LoanType.PERSONAL: 36, LoanType.AUTO: 60, LoanType.HOME: 240}


class LoanArgs(_Args):
    loan_type: LoanType = LoanType.PERSONAL
    # Whole rupees, not paise: small models get minor units wrong by a factor of 100.
    # None (FR-38): no amount was named; the answer is the largest eligible amount, or a question.
    amount_rupees: int | None = Field(default=None, ge=10_000, le=5_00_00_000)
    tenure_months: int | None = Field(default=None, ge=12, le=360)


@dataclass(frozen=True)
class ToolContext:
    """Built by the service from the access token. Tools get identity from here only."""

    session: Session
    customer_id: UUID
    user_id: UUID
    now: datetime
    facts: faq.FaqFacts  # configured limits the help answers quote (TASK-105)


@dataclass(frozen=True)
class ToolReply:
    text: str
    link: str | None = None


class ToolInputError(DomainError):
    """The arguments were valid but do not fit this customer (e.g. ``account=3`` of 2)."""

    status_code = 422
    code = "assistant_tool_input"


@dataclass(frozen=True)
class Tool:
    name: ToolName
    description: str
    args_model: type[_Args]
    handler: Callable[[ToolContext, Mapping[str, object]], ToolReply]


def _own_account(ctx: ToolContext, position: int) -> Account:
    accounts = accounts_service.list_accounts(ctx.session, ctx.customer_id)
    if not accounts:
        raise ToolInputError("You don't have an account yet.")
    if position > len(accounts):
        raise ToolInputError(f"You have {len(accounts)} account(s); pick a number up to that.")
    return accounts[position - 1]


def _label(account: Account) -> str:
    # Last four digits only: a full account number never reaches the chat (Blueprint s10 rule 6).
    return f"{account.account_type.value.title()} \u2022\u2022{account.account_number[-4:]}"


def account_summary(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    AccountSummaryArgs.model_validate(raw)
    accounts = accounts_service.list_accounts(ctx.session, ctx.customer_id)
    if not accounts:
        return ToolReply("You don't have an account yet.", "/accounts")
    lines = [
        f"{i}. {_label(a)}: {format_minor(a.available_balance, a.currency)} available"
        for i, a in enumerate(accounts[:MAX_ACCOUNTS], start=1)
    ]
    return ToolReply("Your accounts:\n" + "\n".join(lines), "/accounts")


def recent_transactions(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    args = RecentTransactionsArgs.model_validate(raw)
    account = _own_account(ctx, args.account)
    page = accounts_service.list_entries_page(
        ctx.session, account, args.limit, None, PageDirection.OLDER
    )
    if not page.items:
        return ToolReply(f"{_label(account)} has no transactions yet.", "/accounts")
    lines = []
    for entry in page.items:
        sign = "-" if entry.direction == "DEBIT" else "+"
        what = untrusted_block(entry.reference) or entry.transaction_type.lower()
        lines.append(
            f"{entry.created_at:%d %b}: {sign}{format_minor(entry.amount_minor, account.currency)}"
            f" ({what})"
        )
    return ToolReply(
        f"Latest {len(lines)} on {_label(account)}:\n" + "\n".join(lines),
        f"/accounts/{account.id}",
    )


# Fixed English names: strftime("%B") would follow the server's locale.
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def _month(now: datetime, period: SpendPeriod) -> tuple[int, int]:
    if period is SpendPeriod.THIS_MONTH:
        return now.year, now.month
    return (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)


def spend(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    args = SpendArgs.model_validate(raw)
    account = _own_account(ctx, args.account)
    year, month = _month(ctx.now, args.period)
    breakdown = analytics_service.get_spending_breakdown(
        ctx.session, ctx.customer_id, account.id, year, month
    )
    money = breakdown.currency
    period = f"{_MONTH_NAMES[month - 1]} {year}"  # "September 2026", not "2026-09"
    if args.category is not None:
        item = next((c for c in breakdown.categories if c.category == args.category.value), None)
        if item is None:
            return ToolReply(f"No {args.category.value} spending in {period}.", "/insights")
        return ToolReply(
            f"In {period} you spent {format_minor(item.amount_minor, money)} on "
            f"{item.category} ({item.transaction_count} transaction(s), "
            f"{item.percentage:.0f}% of spending).",
            "/insights",
        )
    top = sorted(breakdown.categories, key=lambda c: c.amount_minor, reverse=True)[:3]
    tops = ", ".join(f"{c.category} {format_minor(c.amount_minor, money)}" for c in top)
    total = format_minor(breakdown.total_spend_minor, money)
    suffix = f" Top categories: {tops}." if tops else ""
    return ToolReply(f"In {period} you spent {total}.{suffix}", "/insights")


def balance_forecast(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    args = ForecastArgs.model_validate(raw)
    account = _own_account(ctx, args.account)
    forecast = analytics_service.get_balance_forecast(
        ctx.session, ctx.customer_id, account.id, args.horizon_days, ctx.now
    )
    if forecast.status == "INSUFFICIENT_HISTORY" or not forecast.points:
        raise ToolInputError(analytics_service.NOT_ENOUGH_FORECAST_HISTORY)
    last = forecast.points[-1]
    money = forecast.currency
    low, high = format_minor(last.lower_80_minor, money), format_minor(last.upper_80_minor, money)
    return ToolReply(
        f"{forecast.label}: in {args.horizon_days} days {_label(account)} is likely to be about "
        f"{format_minor(last.predicted_minor, money)} (80% range {low} to {high}).",
        "/insights",
    )


def loan_affordability(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    args = LoanArgs.model_validate(raw)
    product = next(p for p in PRODUCTS if p.loan_type == args.loan_type)
    tenure = args.tenure_months or DEFAULT_TENURE_MONTHS[args.loan_type]
    if tenure not in product.tenures_months:
        offered = ", ".join(str(m) for m in product.tenures_months)
        raise ToolInputError(f"A {product.name.lower()} is offered over {offered} months.")
    if args.amount_rupees is None:
        return _loan_suggestion(ctx, product, tenure)
    amount_minor = args.amount_rupees * _PAISE_PER_RUPEE
    if not product.min_amount_minor <= amount_minor <= product.max_amount_minor:
        raise ToolInputError(
            f"A {product.name.lower()} is for {format_minor(product.min_amount_minor)} to "
            f"{format_minor(product.max_amount_minor)}."
        )
    result = loans_service.check_eligibility(
        ctx.session,
        customer_id=ctx.customer_id,
        user_id=ctx.user_id,
        product_id=product.id,
        amount_minor=amount_minor,
        tenure_months=tenure,
        now=ctx.now,
    )
    verdict = "likely to be eligible" if result.status == "ELIGIBLE" else "not eligible right now"
    return ToolReply(
        f"For a {product.name.lower()} of {format_minor(amount_minor)} over "
        f"{tenure} months you are {verdict}. " + " ".join(result.reasons),
        "/loans",
    )


_ASK_EXAMPLE = 'for example: "Can I afford a 2 lakh personal loan over 3 years?"'


def _loan_suggestion(ctx: ToolContext, product: LoanProduct, tenure: int) -> ToolReply:
    """FR-38: no amount named. Offer the TASK-046 maximum, or ask for an amount and say why."""
    found = loans_service.suggest_loan(
        ctx.session,
        customer_id=ctx.customer_id,
        user_id=ctx.user_id,
        product_id=product.id,
        tenure_months=tenure,
        now=ctx.now,
    )
    name, reasons = product.name.lower(), " ".join(found.reasons)
    if found.status == "ASK_AMOUNT":
        return ToolReply(
            f"I can't suggest a {name} amount right now: {reasons} Tell me the amount you have "
            f"in mind and I'll check it, {_ASK_EXAMPLE}",
            "/loans",
        )
    if found.status == "NOT_AFFORDABLE":
        return ToolReply(f"A {name} isn't affordable right now. {reasons}", "/loans")
    rate = f"{found.product.annual_rate_bps // 100}.{found.product.annual_rate_bps % 100:02d}%"
    return ToolReply(
        f"You didn't name an amount, so here is the most you could borrow: a {name} of up to "
        f"{format_minor(found.amount_minor)} over {found.tenure_months} months, an EMI of "
        f"{format_minor(found.emi_minor)} at the catalogue rate from {rate} a year. {reasons} "
        f"This is an estimate, not an offer; ask with an amount to check a different loan.",
        "/loans",
    )


# Fixed wording of the rule (TASK-110); the thresholds come from the analytics service.
def _times(amount: int, usual: int) -> str:
    """The multiple for display only, one decimal place, rounded down (never overstated)."""
    tenths = amount * 10 // usual
    return f"{tenths // 10}.{tenths % 10}x"


_BASELINE_MONTHS = analytics_service.UNUSUAL_BASELINE_MONTHS
_UNUSUAL_RULE = (
    f"Unusual means a category at least {_times(*analytics_service.UNUSUAL_CATEGORY_RATIO)} "
    f"your monthly average of the previous {_BASELINE_MONTHS} months, or a single payment at "
    f"least {_times(*analytics_service.UNUSUAL_TRANSACTION_RATIO)} your usual payment in that "
    "category - and in both cases at least "
    f"{format_minor(analytics_service.UNUSUAL_MIN_EXCESS_MINOR)} more."
)


def _months_label(months: list[tuple[int, int]]) -> str:
    (y1, m1), (y2, m2) = months[0], months[-1]
    return f"{_MONTH_NAMES[m1 - 1]} {y1} to {_MONTH_NAMES[m2 - 1]} {y2}"


def _category_line(item: analytics_service.UnusualCategory, money: str) -> str:
    spent = format_minor(item.spent_minor, money)
    count = f"{item.transaction_count} payment(s)"
    if item.usual_minor == 0:
        return (
            f"- {item.category.title()}: {spent} ({count}); nothing in the previous "
            f"{_BASELINE_MONTHS} months."
        )
    usual = format_minor(item.usual_minor, money)
    more = format_minor(item.spent_minor - item.usual_minor, money)
    times = _times(item.spent_minor, item.usual_minor)
    return (
        f"- {item.category.title()}: {spent} ({count}) against a usual {usual} a month - "
        f"{times}, {more} more."
    )


def _transaction_line(item: analytics_service.UnusualTransaction, money: str) -> str:
    # The reference is written by others (merchant, payer): quoted, stripped, capped (s10).
    what = untrusted_block(item.reference) or "no reference"
    return (
        f"- {item.posted_at:%d %b}: {format_minor(item.amount_minor, money)} {what} "
        f"({item.category}) - {_times(item.amount_minor, item.usual_minor)} your usual "
        f"{item.category} payment of {format_minor(item.usual_minor, money)} "
        f"(median of {item.baseline_count})."
    )


def unusual_expenses(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    args = UnusualExpensesArgs.model_validate(raw)
    account = _own_account(ctx, args.account)
    year, month = _month(ctx.now, args.period)
    result = analytics_service.get_unusual_expenses(
        ctx.session, ctx.customer_id, account.id, year, month
    )
    found, money = result.findings, result.currency
    period = f"{_MONTH_NAMES[month - 1]} {year}"
    baseline = _months_label(result.baseline_months)
    if not found.categories and not found.transactions:
        return ToolReply(
            f"Nothing unusual in {period} on {_label(account)}: every category and payment was "
            f"within your usual range for {baseline}. {_UNUSUAL_RULE}",
            "/insights",
        )
    lines = [f"Unusual spending in {period} on {_label(account)}, compared with {baseline}:"]
    if found.categories:
        lines.append("Categories above your usual level:")
        lines.extend(_category_line(c, money) for c in found.categories)
        if found.more_categories:
            lines.append(f"- and {found.more_categories} more categor(ies).")
    if found.transactions:
        lines.append("Unusually large payments:")
        lines.extend(_transaction_line(t, money) for t in found.transactions)
        if found.more_transactions:
            lines.append(f"- and {found.more_transactions} more payment(s).")
    lines.append(_UNUSUAL_RULE)
    return ToolReply("\n".join(lines), "/insights")


def faq_answer(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    """TASK-105: a curated answer about how PBIP works; no customer data is read."""
    args = FaqArgs.model_validate(raw)
    deposits = (
        wealth_service.list_products(ctx.session, None)
        if args.topic is FaqTopic.FIXED_DEPOSITS
        else []
    )
    found = faq.answer(args.topic, ctx.facts, deposits)
    return ToolReply(found.text, found.link)


_CATEGORY_LINK = {
    ProductCategory.ALL: "/wealth",
    ProductCategory.LOANS: "/loans",
    ProductCategory.DEPOSITS: "/wealth",
    ProductCategory.INVESTMENTS: "/wealth",
}


def list_products(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    """TASK-105: the real loan catalogue and the seeded wealth catalogue, figures as stored."""
    args = ProductsArgs.model_validate(raw)
    wealth = (
        []
        if args.category is ProductCategory.LOANS
        else wealth_service.list_products(ctx.session, None)
    )
    text = discovery.product_list(args.category, PRODUCTS, wealth)
    return ToolReply(text, _CATEGORY_LINK[args.category])


def investment_suggestions(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    """TASK-105: the engine's eligible investment products that fit the customer's risk band.

    Both inputs belong to the customer in :class:`ToolContext` (the access token): the band from
    the TASK-097 questionnaire and the TASK-054 engine output. Without a band nothing is suggested.
    """
    InvestmentSuggestionsArgs.model_validate(raw)
    stored = customers_service.risk_profile(ctx.session, ctx.customer_id)
    if stored.band is None:
        return ToolReply(discovery.NO_PROFILE_TEXT, "/profile")
    profile = discovery.RiskProfile(
        stored.band.value, stored.questionnaire_version, stored.assessed_at
    )
    engine = recommendations_service.get_customer_recommendations(  # ADR-0056: the band rule
        ctx.session, ctx.customer_id, ctx.now, top_k=len(PRODUCT_CATALOGUE), apply_risk_band=False
    )
    suggestions = [
        discovery.EngineSuggestion(item.product_id, item.title, item.reasons)
        for item in engine.recommendations
    ]
    wealth = wealth_service.list_products(ctx.session, None)
    version = engine.model_version  # ADR-0049: a simulated-data model says so in the reply too
    if engine.score_from_simulated_data:
        version += ", a model trained on simulated uptake, not real customer behaviour"
    text = discovery.investment_suggestions(profile, suggestions, version, wealth)
    return ToolReply(text, "/wealth")


REGISTRY: dict[ToolName, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            ToolName.ACCOUNT_SUMMARY,
            "Balances of the customer's own accounts.",
            AccountSummaryArgs,
            account_summary,
        ),
        Tool(
            ToolName.RECENT_TRANSACTIONS,
            "Latest transactions on one of the customer's accounts.",
            RecentTransactionsArgs,
            recent_transactions,
        ),
        Tool(
            ToolName.SPEND,
            "Spending this month or last month, optionally for one category.",
            SpendArgs,
            spend,
        ),
        Tool(
            ToolName.BALANCE_FORECAST,
            "Predicted balance 7, 14 or 30 days ahead, with a range (not exact).",
            ForecastArgs,
            balance_forecast,
        ),
        Tool(
            ToolName.LOAN_AFFORDABILITY,
            "Whether a loan of a given type, amount (whole rupees) and tenure is affordable; "
            "without an amount, the largest affordable amount.",
            LoanArgs,
            loan_affordability,
        ),
        Tool(
            ToolName.UNUSUAL_EXPENSES,
            "Categories and single payments this month or last month that are well above the "
            "customer's own usual level.",
            UnusualExpensesArgs,
            unusual_expenses,
        ),
        Tool(
            ToolName.FAQ_ANSWER,
            "How PBIP works: transfer limits, fraud holds, statements, two-factor sign-in, "
            "sign-in lockout, scheduled transfers, loans or fixed deposits.",
            FaqArgs,
            faq_answer,
        ),
        Tool(
            ToolName.LIST_PRODUCTS,
            "The bank's loans, fixed deposits, funds and bonds with their catalogue rates.",
            ProductsArgs,
            list_products,
        ),
        Tool(
            ToolName.INVESTMENT_SUGGESTIONS,
            "Investment options for the customer from their risk profile and the "
            "recommendation engine.",
            InvestmentSuggestionsArgs,
            investment_suggestions,
        ),
    )
}


def invoke(name: ToolName, ctx: ToolContext, arguments: Mapping[str, object]) -> ToolReply:
    """Run an approved tool. Raises pydantic ``ValidationError`` for out-of-bounds arguments."""
    return REGISTRY[name].handler(ctx, arguments)
