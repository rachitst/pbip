"""Rule-based categorization of bank transactions into financial insights categories."""

import re

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


def categorize_transaction(reference: str | None, tx_type: str | None = None) -> str:
    """Determine spending category for a transaction based on reference, merchant and type."""
    if reference:
        clean_ref = reference.strip()
        for cat_name, pattern in CATEGORY_RULES:
            if pattern.search(clean_ref):
                return cat_name

    if tx_type:
        t = tx_type.upper()
        if "TRANSFER" in t:
            return "transfers"
        if "FEE" in t:
            return "fees"

    return "other"
