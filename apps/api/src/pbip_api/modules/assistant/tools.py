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

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from pbip_api.core.errors import DomainError
from pbip_api.core.money import format_minor
from pbip_api.core.pagination import PageDirection
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.assistant.sanitize import untrusted_block
from pbip_api.modules.loans import service as loans_service
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanType

MAX_ACCOUNTS = 10
MAX_TRANSACTIONS = 20
_PAISE_PER_RUPEE = 100


class ToolName(StrEnum):
    ACCOUNT_SUMMARY = "get_account_summary"
    RECENT_TRANSACTIONS = "get_recent_transactions"
    SPEND = "get_spend"
    BALANCE_FORECAST = "get_balance_forecast"
    LOAN_AFFORDABILITY = "get_loan_affordability"


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
    FEES = "fees"
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


class LoanArgs(_Args):
    loan_type: LoanType = LoanType.PERSONAL
    # Whole rupees, not paise: small models get minor units wrong by a factor of 100.
    amount_rupees: int = Field(ge=10_000, le=5_00_00_000)
    tenure_months: int = Field(ge=12, le=360)


@dataclass(frozen=True)
class ToolContext:
    """Built by the service from the access token. Tools get identity from here only."""

    session: Session
    customer_id: UUID
    user_id: UUID
    now: datetime


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
    if args.category is not None:
        item = next((c for c in breakdown.categories if c.category == args.category.value), None)
        if item is None:
            return ToolReply(
                f"No {args.category.value} spending in {breakdown.period}.", "/insights"
            )
        return ToolReply(
            f"In {breakdown.period} you spent {format_minor(item.amount_minor, money)} on "
            f"{item.category} ({item.transaction_count} transaction(s), "
            f"{item.percentage:.0f}% of spending).",
            "/insights",
        )
    top = sorted(breakdown.categories, key=lambda c: c.amount_minor, reverse=True)[:3]
    tops = ", ".join(f"{c.category} {format_minor(c.amount_minor, money)}" for c in top)
    total = format_minor(breakdown.total_spend_minor, money)
    suffix = f" Top categories: {tops}." if tops else ""
    return ToolReply(f"In {breakdown.period} you spent {total}.{suffix}", "/insights")


def balance_forecast(ctx: ToolContext, raw: Mapping[str, object]) -> ToolReply:
    args = ForecastArgs.model_validate(raw)
    account = _own_account(ctx, args.account)
    forecast = analytics_service.get_balance_forecast(
        ctx.session, ctx.customer_id, account.id, args.horizon_days, ctx.now
    )
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
    amount_minor = args.amount_rupees * _PAISE_PER_RUPEE
    if args.tenure_months not in product.tenures_months:
        offered = ", ".join(str(m) for m in product.tenures_months)
        raise ToolInputError(f"A {product.name.lower()} is offered over {offered} months.")
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
        tenure_months=args.tenure_months,
        now=ctx.now,
    )
    verdict = "likely to be eligible" if result.status == "ELIGIBLE" else "not eligible right now"
    return ToolReply(
        f"For a {product.name.lower()} of {format_minor(amount_minor)} over "
        f"{args.tenure_months} months you are {verdict}. " + " ".join(result.reasons),
        "/loans",
    )


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
            "Whether a loan of a given type, amount (whole rupees) and tenure is affordable.",
            LoanArgs,
            loan_affordability,
        ),
    )
}


def invoke(name: ToolName, ctx: ToolContext, arguments: Mapping[str, object]) -> ToolReply:
    """Run an approved tool. Raises pydantic ``ValidationError`` for out-of-bounds arguments."""
    return REGISTRY[name].handler(ctx, arguments)
