"""Curated, versioned help answers for the assistant's ``get_faq_answer`` tool (TASK-105).

Every answer describes how PBIP actually behaves, and every figure in it is read from the place
that enforces it: limits and lockout settings from :class:`~pbip_api.core.config.Settings`, the
step-up, scheduling and statement bounds from the services that apply them, loan terms from the
loan catalogue and deposit terms from the seeded wealth catalogue. Nothing here is a policy of its
own; ``tests/unit/test_assistant_faq.py`` pins each answer to its source, so a changed limit
changes the answer (or fails the test) instead of leaving the FAQ out of date.

Changing what an answer SAYS (not a figure it reads) is a new :data:`FAQ_VERSION`.
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

FAQ_VERSION = "FAQ-1"
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


_ANSWERS: dict[FaqTopic, Callable[[FaqFacts, Sequence[WealthProductOut]], FaqAnswer]] = {
    FaqTopic.TRANSFER_LIMITS: _transfer_limits,
    FaqTopic.FRAUD_HOLDS: _fraud_holds,
    FaqTopic.STATEMENTS: _statements,
    FaqTopic.MFA: _mfa,
    FaqTopic.SIGN_IN_LOCKOUT: _sign_in_lockout,
    FaqTopic.SCHEDULED_TRANSFERS: _scheduled_transfers,
    FaqTopic.LOANS: _loans,
    FaqTopic.FIXED_DEPOSITS: _fixed_deposits,
}


def answer(
    topic: FaqTopic, facts: FaqFacts, deposits: Sequence[WealthProductOut] = ()
) -> FaqAnswer:
    """The curated answer for ``topic``. ``deposits`` is the wealth catalogue (deposits topic)."""
    return _ANSWERS[topic](facts, deposits)
