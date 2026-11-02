"""Unusual expenses against the customer's OWN baseline (TASK-110). Pure functions, no I/O.

"Unusual" is never a population rule: each customer is compared with themselves.

* **Baseline.** The ``BASELINE_MONTHS`` (3) calendar months immediately before the period month.
  At least ``MIN_BASELINE_MONTHS`` (2) of them must contain spending, otherwise there is no
  usual level to compare with and no item is reported.
* **Unusual category.** The period's spend in a category is at least ``CATEGORY_RATIO`` (1.5x)
  the category's average monthly spend over the baseline (months without spend count as zero),
  and at least ``MIN_EXCESS_MINOR`` (INR 1,000) above it. A category with no baseline spend is
  "new" and is reported when the period spend reaches that same INR 1,000 floor.
* **Unusual transaction.** A single debit at least ``TRANSACTION_RATIO`` (3x) the median debit of
  the same category in the baseline, and at least INR 1,000 above it. The category needs at least
  ``MIN_BASELINE_TRANSACTIONS`` (3) baseline debits for a median to mean anything.

The absolute floor keeps noise out: INR 300 of coffee against a usual INR 100 is 3x but not worth
an alert. The running month is compared with full baseline months, so it can only be flagged once
it has already gone past the usual level (conservative; never a projection). All amounts are
integer minor units and every ratio test is integer arithmetic.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from statistics import median_low

BASELINE_MONTHS = 3
MIN_BASELINE_MONTHS = 2
MIN_BASELINE_TRANSACTIONS = 3
# Ratios as integer fractions (numerator, denominator): 1.5x and 3x.
CATEGORY_RATIO = (3, 2)
TRANSACTION_RATIO = (3, 1)
MIN_EXCESS_MINOR = 1_000_00
MAX_ITEMS = 5


@dataclass(frozen=True)
class Debit:
    """One spend debit of the account: what was paid, when, and to what (untrusted text)."""

    amount_minor: int
    category: str
    reference: str | None
    posted_at: datetime


@dataclass(frozen=True)
class UnusualCategory:
    category: str
    spent_minor: int  # in the period
    usual_minor: int  # average month of the baseline (0 = nothing in the baseline months)
    transaction_count: int  # in the period


@dataclass(frozen=True)
class UnusualTransaction:
    amount_minor: int
    category: str
    reference: str | None  # untrusted: the caller must delimit, strip and cap it before display
    posted_at: datetime
    usual_minor: int  # median debit of this category in the baseline
    baseline_count: int  # how many baseline debits that median is taken over


@dataclass(frozen=True)
class UnusualFindings:
    categories: list[UnusualCategory]  # largest excess first, at most MAX_ITEMS
    transactions: list[UnusualTransaction]  # largest multiple of the usual first, at most MAX_ITEMS
    more_categories: int  # flagged but not listed (over MAX_ITEMS)
    more_transactions: int


def _at_least(value: int, base: int, ratio: tuple[int, int]) -> bool:
    numerator, denominator = ratio
    return value * denominator >= base * numerator


def _by_category(debits: Sequence[Debit]) -> dict[str, list[Debit]]:
    out: dict[str, list[Debit]] = {}
    for debit in debits:
        out.setdefault(debit.category, []).append(debit)
    return out


def unusual_categories(
    period: Sequence[Debit], baseline: Sequence[Debit], months: int = BASELINE_MONTHS
) -> list[UnusualCategory]:
    """Every category of the period whose spend is well above its baseline monthly average."""
    usual_total = {
        cat: sum(d.amount_minor for d in ds) for cat, ds in _by_category(baseline).items()
    }
    found = []
    for category, debits in _by_category(period).items():
        spent = sum(d.amount_minor for d in debits)
        usual = usual_total.get(category, 0) // months
        if spent - usual >= MIN_EXCESS_MINOR and _at_least(spent, usual, CATEGORY_RATIO):
            found.append(UnusualCategory(category, spent, usual, len(debits)))
    return sorted(found, key=lambda c: (-(c.spent_minor - c.usual_minor), c.category))


def unusual_transactions(
    period: Sequence[Debit], baseline: Sequence[Debit]
) -> list[UnusualTransaction]:
    """Every debit of the period that is well above the median debit of its category."""
    medians = {
        cat: (median_low([d.amount_minor for d in ds]), len(ds))
        for cat, ds in _by_category(baseline).items()
        if len(ds) >= MIN_BASELINE_TRANSACTIONS
    }
    found = []
    for debit in period:
        if debit.category not in medians:
            continue
        usual, count = medians[debit.category]
        excess = debit.amount_minor - usual
        if excess >= MIN_EXCESS_MINOR and _at_least(debit.amount_minor, usual, TRANSACTION_RATIO):
            found.append(
                UnusualTransaction(
                    debit.amount_minor,
                    debit.category,
                    debit.reference,
                    debit.posted_at,
                    usual,
                    count,
                )
            )
    # Largest multiple of the usual first (exact fractions, no floats), then the newest.
    return sorted(
        found, key=lambda t: (Fraction(t.amount_minor, t.usual_minor), t.posted_at), reverse=True
    )


def baseline_months_with_spend(baseline: Sequence[Debit]) -> int:
    return len({(d.posted_at.year, d.posted_at.month) for d in baseline})


def find_unusual(period: Sequence[Debit], baseline: Sequence[Debit]) -> UnusualFindings:
    categories = unusual_categories(period, baseline)
    transactions = unusual_transactions(period, baseline)
    return UnusualFindings(
        categories=categories[:MAX_ITEMS],
        transactions=transactions[:MAX_ITEMS],
        more_categories=max(len(categories) - MAX_ITEMS, 0),
        more_transactions=max(len(transactions) - MAX_ITEMS, 0),
    )
