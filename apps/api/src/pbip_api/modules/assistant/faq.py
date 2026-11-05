"""Curated, versioned help answers for the assistant's ``get_faq_answer`` tool (TASK-105).

Every answer describes how PBIP actually behaves, and every figure in it is read from the place
that enforces it: limits and lockout settings from :class:`~pbip_api.core.config.Settings`, the
step-up, scheduling and statement bounds from the services that apply them, loan terms from the
loan catalogue and deposit terms from the seeded wealth catalogue. Nothing here is a policy of its
own; ``tests/unit/test_assistant_faq.py`` pins each answer to its source, so a changed limit
changes the answer (or fails the test) instead of leaving the FAQ out of date.

Changing what an answer SAYS (not a figure it reads) is a new :data:`FAQ_VERSION`.

:data:`FEATURE_TOPICS` maps every customer feature to the topic that explains it; a unit test fails
when a feature has no entry. Investing (funds, stocks, bonds) and insurance are SIMULATED in PBIP,
and their answers say so in general terms that stay true as those flows grow.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pbip_api.core.config import Settings
from pbip_api.core.money import format_minor
from pbip_api.modules.assistant.sanitize import untrusted_block
from pbip_api.modules.loans.catalogue import PRODUCTS as LOAN_PRODUCTS
from pbip_api.modules.loans.service import LOCKOUT_LOOKBACK, MAX_DTI, MAX_SPENDING_PERCENT
from pbip_api.modules.payments.service import (
    SCHEDULE_MAX_LEAD_DAYS,
    STEP_UP_MAX_FAILURES,
)
from pbip_api.modules.statements.service import MAX_ENTRIES, MAX_PERIOD_DAYS
from pbip_api.modules.wealth.deposit_math import NO_INTEREST_DAYS
from pbip_api.modules.wealth.schemas import WealthProductOut

FAQ_VERSION = "FAQ-2"  # FAQ-2: an entry for every customer feature (section 5, UC-08)
# auth.service caps a lockout at one hour (_MAX_LOCK_SECONDS); the unit test pins this wording.
LOCK_CAP_TEXT = "an hour"


class FaqTopic(StrEnum):
    TRANSFER_LIMITS = "transfer_limits"
    FRAUD_HOLDS = "fraud_holds"
    STATEMENTS = "statements"
    MFA = "mfa"
    SIGN_IN_LOCKOUT = "sign_in_lockout"
    SCHEDULED_TRANSFERS = "scheduled_transfers"
    LOANS = "loans"
    FIXED_DEPOSITS = "fixed_deposits"
    ACCOUNTS = "accounts"
    PAYEES = "payees"
    TRANSFERS = "transfers"
    BILL_PAYMENTS = "bill_payments"
    INVESTMENTS = "investments"
    INSURANCE = "insurance"
    NOTIFICATIONS = "notifications"
    ACCOUNT_SECURITY = "account_security"
    SETTINGS = "settings"
    CONSENT = "consent"
    SPENDING_INSIGHTS = "spending_insights"
    ASSISTANT = "assistant"


# Every customer-facing feature and the topic that explains it (tested for completeness).
FEATURE_TOPICS: dict[str, FaqTopic] = {
    "accounts": FaqTopic.ACCOUNTS,
    "statements": FaqTopic.STATEMENTS,
    "payees": FaqTopic.PAYEES,
    "transfers": FaqTopic.TRANSFERS,
    "transfer limits": FaqTopic.TRANSFER_LIMITS,
    "fraud holds": FaqTopic.FRAUD_HOLDS,
    "scheduled transfers": FaqTopic.SCHEDULED_TRANSFERS,
    "bill payments": FaqTopic.BILL_PAYMENTS,
    "loans": FaqTopic.LOANS,
    "fixed deposits": FaqTopic.FIXED_DEPOSITS,
    "funds, stocks and bonds": FaqTopic.INVESTMENTS,
    "insurance": FaqTopic.INSURANCE,
    "notifications": FaqTopic.NOTIFICATIONS,
    "two-factor sign-in": FaqTopic.MFA,
    "sign-in lockout": FaqTopic.SIGN_IN_LOCKOUT,
    "devices and password": FaqTopic.ACCOUNT_SECURITY,
    "settings": FaqTopic.SETTINGS,
    "consent": FaqTopic.CONSENT,
    "spending and financial health": FaqTopic.SPENDING_INSIGHTS,
    "assistant limits": FaqTopic.ASSISTANT,
}


@dataclass(frozen=True)
class FaqFacts:
    """The configured figures the answers quote (from the validated settings, never defaults)."""

    max_transfer_minor: int
    daily_transfer_limit_minor: int
    login_max_failures: int
    login_lock_base_seconds: int
    max_scheduled_transfers: int

    @classmethod
    def from_settings(cls, limits: Settings) -> "FaqFacts":
        """``limits`` must be the effective settings (``runtime_config.effective_settings``), so the
        FAQ quotes the limits the services enforce, including an ADMIN's overrides (ADR-0031)."""
        return cls(
            max_transfer_minor=limits.max_transfer_minor,
            daily_transfer_limit_minor=limits.daily_transfer_limit_minor,
            login_max_failures=limits.login_max_failures,
            login_lock_base_seconds=limits.login_lock_base_seconds,
            max_scheduled_transfers=limits.max_active_scheduled_transfers_per_customer,
        )


@dataclass(frozen=True)
class FaqAnswer:
    text: str
    link: str


def percent(bps: int) -> str:
    """Basis points as a percentage with two decimals, exactly (1150 -> "11.50%")."""
    return f"{bps // 100}.{bps % 100:02d}%"


def _months(values: Sequence[int]) -> str:
    if len(values) == 1:
        return f"{values[0]} months"
    return f"{values[0]} to {values[-1]} months"


def _transfer_limits(facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        f"One transfer or bill payment can be up to {format_minor(facts.max_transfer_minor)}, "
        f"and you can send up to {format_minor(facts.daily_transfer_limit_minor)} in total per "
        "day. Transfers and bill payments that are posted or still waiting for verification or "
        "review count towards the daily total, which starts again at 00:00 UTC (05:30 IST).",
        "/transfers",
    )


def _fraud_holds(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Every transfer is checked for fraud before any money moves. It then goes through, asks "
        "for a code from your authenticator app, is held for review by our fraud team, or is "
        "declined. While a transfer is under review the amount is held: it stays in your account "
        "but is not available to spend. If the review approves it, it is paid; if not, the hold "
        f"is released. After {STEP_UP_MAX_FAILURES} wrong codes a transfer goes to review, and "
        "so does one that would ask for a code when two-factor sign-in is off or the transfer is "
        "a scheduled one. You are notified when a transfer is paid, held for review or declined. "
        "For your protection, the reasons a "
        "payment was flagged are not shown.",
        "/transfers",
    )


def _statements(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Open Statements, choose one of your accounts and a period, and download it as PDF or "
        f"CSV. A statement covers up to {MAX_PERIOD_DAYS} days (dates in IST), cannot end in "
        f"the future and can hold up to {MAX_ENTRIES:,} transactions; choose a shorter period "
        "if you have more. Each download is recorded and you get a notification that a "
        "statement was downloaded.",
        "/statements",
    )


def _mfa(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Turn on two-factor sign-in under Profile & security: scan the code with an "
        "authenticator app and enter the 6-digit code it shows (a new one every 30 seconds). "
        "You then get one-time recovery codes; keep them safe, each works once if you lose your "
        "phone. Signing in then asks for a code, each code can be used only once, and a risky "
        "transfer can ask for one too.",
        "/profile",
    )


def _sign_in_lockout(facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    lookback = LOCKOUT_LOOKBACK.days
    return FaqAnswer(
        f"After {facts.login_max_failures} failed sign-in attempts in a row your sign-in is "
        f"locked for {facts.login_lock_base_seconds} seconds, and each further failure doubles "
        f"the wait, up to {LOCK_CAP_TEXT}. Wrong verification codes count as failures too; a "
        f"correct password or code resets the count. A lockout in the last {lookback} days also "
        f"pauses loan checks until {lookback} days after it.",
        "/profile",
    )


def _scheduled_transfers(facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Under Scheduled transfers you can pay a saved payee or one of your own accounts daily, "
        f"weekly or monthly, starting within {SCHEDULE_MAX_LEAD_DAYS} days, with an optional end "
        "date. A monthly transfer keeps its day of the month and runs on the last day of shorter "
        "months. Each run is checked like any transfer (fraud checks and limits). If money is "
        "short or the daily limit is reached you are notified; a run blocked as suspected fraud "
        "pauses the order. Runs missed while the service was unavailable are not paid late: you "
        "are told, and the order continues from its next date. You can pause, resume or cancel "
        f"an order, and have up to {facts.max_scheduled_transfers} active or paused at once.",
        "/scheduled",
    )


def _loan_line(index: int) -> str:
    product = LOAN_PRODUCTS[index]
    return (
        f"{product.name.lower()} from {percent(product.annual_rate_bps)} a year "
        f"({format_minor(product.min_amount_minor)} to {format_minor(product.max_amount_minor)}, "
        f"{_months(product.tenures_months)})"
    )


def _loans(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    offered = "; ".join(_loan_line(i) for i in range(len(LOAN_PRODUCTS)))
    return FaqAnswer(
        f"PBIP offers a {offered}. Rates are indicative and subject to eligibility. Eligibility "
        "uses your own account history: your regular monthly income, your spending, your balance "
        f"and the loan repayments you already make. All repayments, including the new EMI, must "
        f"stay within {MAX_DTI:.0%} of your income, your spending plus the new EMI within "
        f"{MAX_SPENDING_PERCENT}% of it, your balance must cover the first EMI, and a sign-in "
        f"lockout in the last "
        f"{LOCKOUT_LOOKBACK.days} days pauses loan checks. An approved loan is paid into your "
        "account and repaid by a monthly instruction that is set up for you.",
        "/loans",
    )


def _deposit_line(product: WealthProductOut) -> str:
    rate = percent(product.annual_rate_bps or 0)
    line = f"{untrusted_block(product.name)} at {rate} a year"
    if product.min_lock_in_months:
        return f"{line}, locked in for {product.min_lock_in_months} months"
    return line


def _fixed_deposits(_facts: FaqFacts, deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    bookable = [p for p in deposits if p.bookable and p.annual_rate_bps is not None]
    if not bookable:
        return FaqAnswer("No fixed deposits are offered at the moment.", "/wealth")
    penalties = {p.premature_penalty_bps for p in bookable if p.premature_penalty_bps is not None}
    early = (
        f"Closing early, where allowed, pays the rate less {percent(min(penalties))} for the "
        f"time held, and no interest within the first {NO_INTEREST_DAYS} days. "
        if len(penalties) == 1
        else f"Closing early, where allowed, pays a reduced rate for the time held, and no "
        f"interest within the first {NO_INTEREST_DAYS} days. "
    )
    return FaqAnswer(
        "Book a fixed deposit from one of your accounts on the Wealth page; the money moves at "
        "once and earns a fixed rate, compounded quarterly and paid with the principal at "
        "maturity. On offer: "
        + "; ".join(_deposit_line(p) for p in bookable)
        + ". "
        + early
        + "Rates are indicative, simulated PBIP rates, not a real bank's offer.",
        "/wealth",
    )


def _accounts(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Accounts lists each of your accounts with its available balance, any amount on hold "
        "and its latest transactions. You can open another savings or current (checking) "
        "account from the same page. Money on hold (for example a transfer under review) stays "
        "in the account but cannot be spent until the hold is released or the payment is made.",
        "/accounts",
    )


def _payees(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Add a payee under Payees with their name exactly as on their bank account, their "
        "account number and bank code. For an account at PBIP the name is checked against the "
        "account holder; an account at another bank is verified without a name check. You can "
        "pay a payee once it is verified, give it a nickname, edit it or remove it. A large "
        "payment to a payee you added recently may ask for a code from your authenticator app.",
        "/payees",
    )


def _transfers(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "To send money, open Accounts, choose the account to pay from, a verified payee, the "
        "amount and an optional reference, then review and confirm. Every transfer is checked "
        "for fraud before money moves and counts towards your limits; it is then paid, asks for "
        "an authenticator code, or is held for review. Ask me about transfer limits or fraud "
        "holds for the details. I can't send money for you.",
        "/accounts",
    )


def _bill_payments(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Under Pay bills choose the account to pay from, a biller (such as electricity, water, "
        "gas, phone, broadband or an insurance premium), the consumer number printed on your "
        "bill and the amount, then review and confirm. A bill payment is checked for fraud and "
        "counts towards your daily limit like a transfer, and your recent bill payments are "
        "listed on the same page.",
        "/bills",
    )


def _investments(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Mutual funds, stocks and bonds in PBIP are simulated: prices, returns and any holdings "
        "are illustrative, no real securities are bought or sold, and nothing is traded on a "
        "real market. The Wealth page shows what is offered and lets you compare products. "
        "Market-linked products can lose value. Answer the risk questionnaire under Profile & "
        "security so suggestions fit your risk profile; they are not personalised financial "
        "advice.",
        "/wealth",
    )


def _insurance(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Insurance in PBIP (term life and health cover) is simulated: no real insurer issues a "
        "policy and nothing is underwritten. The Wealth page lists the plans with their cover, "
        "terms and entry ages, and an indicative premium stated for one example customer, not a "
        "quote for you. Eligibility hints come from each plan's own entry ages; a real insurer "
        "would decide eligibility and the premium.",
        "/wealth",
    )


def _notifications(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "The bell at the top of every page shows your notifications and how many are unread: "
        "payments made, held or declined, scheduled transfers that could not run, loans, fixed "
        "deposits, statement downloads, a low balance ahead, and security changes such as a new "
        "password, two-factor sign-in or a new device. Opening one marks it read and, where "
        "there is one, takes you to the related page; you can also mark them all read.",
        "/accounts",
    )


def _account_security(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Under Profile & security you can change your password, turn on two-factor sign-in and "
        "see the devices you sign in from; a new device is untrusted until it builds history. "
        "PBIP never asks for your password or an authenticator code by phone, message or email, "
        "and the assistant never needs them. If something looks wrong, change your password.",
        "/profile",
    )


def _settings(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Profile & security shows your details. There you can add or change an optional mobile "
        "number (stored encrypted, only its last four digits are shown), your date of birth and "
        "recommendations consent, your risk-tolerance answers and your password. The light or "
        "dark theme switch is at the top of the page.",
        "/profile",
    )


def _consent(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "PBIP asks for two separate, optional consents, and you can withdraw either at any time "
        "under Profile & security. Recommendations: with your date of birth and consent, only "
        "your age band is used to offer products you are eligible for, such as senior-citizen "
        "deposit rates; it is never used to price credit, and staff and the assistant never see "
        "the date. Usage analytics: how you use the app (pages and product interest) is recorded "
        "only while you agree; it is off until you turn it on, and withdrawing it erases what "
        "was recorded.",
        "/profile",
    )


def _spending_insights(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "Spending & health shows your spending by category, how it changed from month to month, "
        "a financial health score with the reasons behind it, and a forecast of your balance "
        "with a range, not an exact figure. All of it is worked out from your own transactions. "
        "You can also ask me what you spent, about unusual expenses or for a forecast.",
        "/insights",
    )


def _assistant(_facts: FaqFacts, _deposits: Sequence[WealthProductOut]) -> FaqAnswer:
    return FaqAnswer(
        "I can read your own balances, transactions, spending and forecast, check loan "
        "affordability, list the bank's products and answer questions about how PBIP works. I "
        "only read: I can't move money, pay bills, change settings or see anyone else's "
        "information. Messages are up to {MAX_MESSAGE_CHARS} characters. My answers come from "
        "the bank's own figures and are not personalised financial advice; when the language "
        "model is unavailable I answer simpler questions only.",
        "/accounts",
    )


_ANSWERS: dict[FaqTopic, Callable[[FaqFacts, Sequence[WealthProductOut]], FaqAnswer]] = {
    FaqTopic.TRANSFER_LIMITS: _transfer_limits,
    FaqTopic.FRAUD_HOLDS: _fraud_holds,
    FaqTopic.STATEMENTS: _statements,
    FaqTopic.MFA: _mfa,
    FaqTopic.SIGN_IN_LOCKOUT: _sign_in_lockout,
    FaqTopic.SCHEDULED_TRANSFERS: _scheduled_transfers,
    FaqTopic.LOANS: _loans,
    FaqTopic.FIXED_DEPOSITS: _fixed_deposits,
    FaqTopic.ACCOUNTS: _accounts,
    FaqTopic.PAYEES: _payees,
    FaqTopic.TRANSFERS: _transfers,
    FaqTopic.BILL_PAYMENTS: _bill_payments,
    FaqTopic.INVESTMENTS: _investments,
    FaqTopic.INSURANCE: _insurance,
    FaqTopic.NOTIFICATIONS: _notifications,
    FaqTopic.ACCOUNT_SECURITY: _account_security,
    FaqTopic.SETTINGS: _settings,
    FaqTopic.CONSENT: _consent,
    FaqTopic.SPENDING_INSIGHTS: _spending_insights,
    FaqTopic.ASSISTANT: _assistant,
}


def answer(
    topic: FaqTopic, facts: FaqFacts, deposits: Sequence[WealthProductOut] = ()
) -> FaqAnswer:
    """The curated answer for ``topic``. ``deposits`` is the wealth catalogue (deposits topic)."""
    return _ANSWERS[topic](facts, deposits)
