"""Deterministic keyword/regex intent router (TASK-058): the assistant without a model.

Two uses, same rules (a rule set, not a model):

* :func:`clear_route` - the FAST PATH. A message that names exactly one intent with usable
  arguments ("what's my balance", "groceries last month") goes straight to its tool; no LLM call.
* :func:`route` - the FALLBACK when Ollama is off, slow (past ``OLLAMA_TIMEOUT_SECONDS``) or
  returns anything that does not validate. Order of precedence picks one intent.

Same output shape as the LLM router, so the rest of the pipeline cannot tell the difference.
"""

import re
from dataclasses import dataclass, field

from pbip_api.modules.assistant.tools import (
    MAX_ACCOUNTS,
    MAX_TRANSACTIONS,
    SpendCategory,
    SpendPeriod,
    ToolName,
)
from pbip_api.modules.loans.catalogue import LoanType


@dataclass(frozen=True)
class Route:
    """``tool=None`` means: no tool fits; answer with help text."""

    tool: ToolName | None
    arguments: dict[str, object] = field(default_factory=dict)


_I = re.IGNORECASE
_FORECAST = re.compile(
    r"\b(forecast|predict|projection|project(ed)?|future|end of (the )?month|next (week|month)"
    r"|will (i|my)\b.*\b(have|balance)|going to have)\b",
    _I,
)
_LOAN = re.compile(r"\b(loan|emi|borrow|mortgage|afford)\b", _I)
_TRANSACTIONS = re.compile(r"\b(transactions?|recent|history|statement|last \d+)\b", _I)
_SPEND = re.compile(r"\b(spend|spent|spending|expenses?|expenditure|cost me)\b", _I)
_BALANCE = re.compile(r"\b(balance|accounts?|how much (money )?(do i have|is in|have i got))\b", _I)
# Mentions of other people: never fast-pathed, so "balances of all customers" is not answered
# with the caller's own balance as if that were what they asked (the guard refuses the clear
# cross-customer requests; the model judges the rest).
_THIRD_PARTY = re.compile(
    r"\b(customers?|users?|everyone|everybody|people|someone|somebody|others?|their|his|her)\b", _I
)
_LAST_MONTH = re.compile(r"\b(last|previous|past) month\b", _I)
# Whitespace runs and digit groups are BOUNDED ({0,3}, {1,12}): unbounded \s* / nested quantifiers
# on customer text are polynomial-time regexes (CodeQL py/polynomial-redos).
_COUNT = re.compile(
    r"\b(?:last|latest|recent|past)?\s{0,3}(\d{1,3})\s{0,3}(?:transactions?|txns?)?\b", _I
)
_ACCOUNT_NO = re.compile(r"\baccount(?:\s{1,3}(?:no\.?|number|#))?\s{0,3}(\d{1,2})\b", _I)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
_ORDINAL = re.compile(r"\b(first|second|third|fourth|fifth)\s+account\b", _I)
_AMOUNT = re.compile(
    r"(?:₹|\brs\.?|\binr)?\s{0,3}(\d{1,12}(?:[.,]\d{1,3}){0,4})\s{0,3}"
    r"(lakh|lakhs|lac|l|crore|crores|cr|k|thousand)?\b",
    _I,
)
_YEARS = re.compile(r"\b(\d{1,2})\s{0,3}(?:years?|yrs?)\b", _I)
_MONTHS = re.compile(r"\b(\d{1,3})\s{0,3}(?:months?|mths?)\b", _I)
_TENURE_AFTER = re.compile(r"\s{0,3}(years?|yrs?|months?|mths?)\b", _I)
_SCALE = {
    "lakh": 1_00_000,
    "lakhs": 1_00_000,
    "lac": 1_00_000,
    "l": 1_00_000,
    "crore": 1_00_00_000,
    "crores": 1_00_00_000,
    "cr": 1_00_00_000,
    "k": 1_000,
    "thousand": 1_000,
}
_CATEGORY_WORDS: tuple[tuple[SpendCategory, re.Pattern[str]], ...] = tuple(
    (cat, re.compile(pattern, _I))
    for cat, pattern in (
        (SpendCategory.GROCERIES, r"\bgrocer(y|ies)\b|\bsupermarket\b"),
        (SpendCategory.DINING, r"\b(dining|restaurants?|food|eating out|takeaway|swiggy|zomato)\b"),
        (SpendCategory.UTILITIES, r"\b(utilit(y|ies)|electricity|water|gas bill|internet|phone)\b"),
        (SpendCategory.TRANSPORT, r"\b(transport|fuel|petrol|uber|ola|taxi|metro|commute)\b"),
        (SpendCategory.SHOPPING, r"\b(shopping|clothes|amazon|flipkart)\b"),
        (SpendCategory.ENTERTAINMENT, r"\b(entertainment|movies?|netflix|streaming)\b"),
        (SpendCategory.TRAVEL, r"\b(travel|flights?|hotels?|holiday|trip)\b"),
        (SpendCategory.HEALTHCARE, r"\b(health|healthcare|medical|doctor|pharmacy|hospital)\b"),
        (SpendCategory.EDUCATION, r"\b(education|school|college|tuition|course)\b"),
        (SpendCategory.HOUSING, r"\b(housing|rent|maintenance)\b"),
        (SpendCategory.FEES, r"\b(fees?|charges?)\b"),
    )
)
_DEFAULT_TENURE = {LoanType.PERSONAL: 36, LoanType.AUTO: 60, LoanType.HOME: 240}


def _account(message: str) -> int:
    ordinal = _ORDINAL.search(message)
    if ordinal:
        return _ORDINALS[ordinal.group(1).lower()]
    number = _ACCOUNT_NO.search(message)
    if number:
        return min(max(int(number.group(1)), 1), MAX_ACCOUNTS)
    return 1


def _with_account(message: str, arguments: dict[str, object]) -> dict[str, object]:
    position = _account(message)
    return arguments if position == 1 else {**arguments, "account": position}


def _horizon(message: str) -> int:
    lowered = message.lower()
    if "week" in lowered and "two week" not in lowered and "2 week" not in lowered:
        return 7
    if "fortnight" in lowered or "two week" in lowered or "2 week" in lowered:
        return 14
    return 30


def parse_rupees(message: str) -> int | None:
    """Largest amount mentioned, in whole rupees ("5 lakh" -> 500000, "1.5 cr" -> 15000000)."""
    best: int | None = None
    for match in _AMOUNT.finditer(message):
        digits, unit = match.group(1).replace(",", ""), (match.group(2) or "").lower()
        if _is_tenure(message, match.end()):
            continue
        try:
            value = float(digits)
        except ValueError:
            continue
        rupees = round(value * _SCALE.get(unit, 1))
        if best is None or rupees > best:
            best = rupees
    return best


def _is_tenure(message: str, end: int) -> bool:
    return bool(_TENURE_AFTER.match(message, end))


def _loan(message: str) -> Route:
    lowered = message.lower()
    if re.search(r"\b(home|house|flat|mortgage|property)\b", lowered):
        loan_type = LoanType.HOME
    elif re.search(r"\b(car|auto|vehicle|bike)\b", lowered):
        loan_type = LoanType.AUTO
    else:
        loan_type = LoanType.PERSONAL
    years, months = _YEARS.search(message), _MONTHS.search(message)
    tenure = (
        int(years.group(1)) * 12
        if years
        else int(months.group(1))
        if months
        else _DEFAULT_TENURE[loan_type]
    )
    amount = parse_rupees(message)
    if amount is None:
        return Route(None)
    return Route(
        ToolName.LOAN_AFFORDABILITY,
        {"loan_type": loan_type.value, "amount_rupees": amount, "tenure_months": tenure},
    )


def _transactions(message: str) -> Route:
    limit = 5
    for match in _COUNT.finditer(message):
        if re.search(r"transaction|txn|last|latest|recent", match.group(0), _I):
            limit = min(max(int(match.group(1)), 1), MAX_TRANSACTIONS)
            break
    return Route(ToolName.RECENT_TRANSACTIONS, _with_account(message, {"limit": limit}))


def _spend(message: str) -> Route:
    period = SpendPeriod.LAST_MONTH if _LAST_MONTH.search(message) else SpendPeriod.THIS_MONTH
    arguments: dict[str, object] = {"period": period.value}
    category = next((cat for cat, rx in _CATEGORY_WORDS if rx.search(message)), None)
    if category is not None:
        arguments["category"] = category.value
    return Route(ToolName.SPEND, _with_account(message, arguments))


def _intents(message: str) -> list[ToolName]:
    """Every intent whose keywords appear, most specific first."""
    found = [
        tool
        for tool, rx in (
            (ToolName.BALANCE_FORECAST, _FORECAST),
            (ToolName.LOAN_AFFORDABILITY, _LOAN),
            (ToolName.RECENT_TRANSACTIONS, _TRANSACTIONS),
            (ToolName.SPEND, _SPEND),
        )
        if rx.search(message)
    ]
    # "account" appears in questions about anything ("spend on my second account"), so the
    # balance intent counts only when nothing more specific was named.
    if not found and _BALANCE.search(message):
        found.append(ToolName.ACCOUNT_SUMMARY)
    return found


def clear_route(message: str) -> Route | None:
    """The fast path: exactly one intent and its arguments parse. Otherwise None (ask the LLM).

    "spending and recent transactions" names two intents, "can I get a loan?" has no amount:
    both are unclear and go to the model (or, without one, to :func:`route`).
    """
    if len(_intents(message)) != 1 or _THIRD_PARTY.search(message):
        return None
    chosen = route(message)
    return chosen if chosen.tool is not None else None


def route(message: str) -> Route:
    """Pick a tool by keywords. Order matters: the most specific intent wins."""
    if _FORECAST.search(message):
        return Route(
            ToolName.BALANCE_FORECAST, _with_account(message, {"horizon_days": _horizon(message)})
        )
    if _LOAN.search(message):
        return _loan(message)
    if _TRANSACTIONS.search(message):
        return _transactions(message)
    if _SPEND.search(message):
        return _spend(message)
    if _BALANCE.search(message):
        return Route(ToolName.ACCOUNT_SUMMARY)
    return Route(None)
