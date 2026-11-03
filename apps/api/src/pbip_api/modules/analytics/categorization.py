"""Rule-based categorization of bank transactions into financial insights categories."""

import re
from functools import lru_cache

CATEGORY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "groceries",
        re.compile(
            r"\b(grocer|supermarket|mart|bigbasket|blinkit|zepto|vegetable|fruit|dairy|provisions)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "utilities",
        re.compile(
            r"\b(utility|electric|water|gas|bescom|airtel|jio|broadband|wifi|power|bill|dth|recharge)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "dining",
        re.compile(
            r"\b(restaurant|cafe|swiggy|zomato|dining|food|mcdonald|starbucks|pizza|burger|bakery|coffee)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "transport",
        re.compile(
            r"\b(uber|ola|metro|fuel|petrol|diesel|transport|train|irctc|bus|taxi|toll|parking|flight|airline)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "shopping",
        re.compile(
            r"\b(amazon|flipkart|myntra|shopping|retail|cloth|fashion|apparel|electronics|mall|store)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "entertainment",
        re.compile(
            r"\b(netflix|spotify|cinema|movie|pvr|inox|entertainment|game|gaming|hotstar|prime|theatre|concert)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "travel",
        re.compile(
            r"\b(hotel|airbnb|booking|travel|makemytrip|goibibo|resort|vacation|trip|hostel)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "healthcare",
        re.compile(
            r"\b(hospital|pharmacy|chemist|medical|doctor|health|apollo|1mg|clinic|dental|diagnostic)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "education",
        re.compile(
            r"\b(school|college|course|udemy|coursera|tuition|education|university|exam|books|library)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "housing",
        re.compile(
            r"\b(rent|maintenance|housing|society|apartment|flat|mortgage|landlord)\b",
            re.IGNORECASE,
        ),
    ),
)


DEBT_REPAYMENT = re.compile(
    r"\b(emis?|loans?|mortgages?|instal?lments?|repayments?)\b", re.IGNORECASE
)


def is_debt_repayment(reference: str | None) -> bool:
    """Whether a debit's reference marks a loan or EMI instalment (word boundaries, so that
    "chemist", "premium" or "remit" do not count as debt)."""
    return reference is not None and DEBT_REPAYMENT.search(reference) is not None


# A pure function of two short strings, called once per debit on every analytics and
# recommendation read: references repeat (the same merchants every month), so the regex scan runs
# once per distinct pair (#343). Bounded, so it cannot grow with untrusted reference text.
CATEGORY_CACHE_SIZE = 4096


@lru_cache(maxsize=CATEGORY_CACHE_SIZE)
def categorize_transaction(reference: str | None, tx_type: str | None = None) -> str:
    """Determine spending category for a transaction based on reference, merchant and type."""
    if reference:
        clean_ref = reference.strip()
        for cat_name, pattern in CATEGORY_RULES:
            if pattern.search(clean_ref):
                return cat_name

    if tx_type and "TRANSFER" in tx_type.upper():
        return "transfers"

    return "other"
