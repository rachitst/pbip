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

from pbip_api.modules.assistant.discovery import ProductCategory
from pbip_api.modules.assistant.faq import FaqTopic
from pbip_api.modules.assistant.tools import (
    DEFAULT_TENURE_MONTHS,
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
# A loan is named (not just "afford"): only then is "how much can I borrow?" answered without an
# amount (FR-38). "Can I afford a holiday?" stays unanswered rather than becoming a loan quote.
_LOAN_NAMED = re.compile(r"\b(loans?|emis?|borrow(ing)?|mortgage|finance)\b", _I)
_UNUSUAL = re.compile(
    r"\b(unusual(ly)?|unexpected|abnormal|anomal(y|ies|ous)|out of the ordinary|spik(e|es|ed)"
    r"|higher than (usual|normal)|more than (usual|normal)|bigger than (usual|normal))\b",
    _I,
)
_TRANSACTIONS = re.compile(r"\b(transactions?|recent|history|statement|last \d+)\b", _I)
_SPEND = re.compile(r"\b(spend|spent|spending|expenses?|expenditure|cost me)\b", _I)
_BALANCE = re.compile(r"\b(balance|accounts?|how much (money )?(do i have|is in|have i got))\b", _I)
# Mentions of other people: never fast-pathed, so "balances of all customers" is not answered
# with the caller's own balance as if that were what they asked (the guard refuses the clear
# cross-customer requests; the model judges the rest).
_THIRD_PARTY = re.compile(
    r"\b(customers?|users?|everyone|everybody|people|someone|somebody|others?|their|his|her)\b", _I
)
# TASK-105. Help questions name a topic of how the bank works, never the customer's own data.
# All quantifiers are bounded ({0,40}) for the same ReDoS reason as below.
_FAQ: tuple[tuple[FaqTopic, re.Pattern[str]], ...] = tuple(
    (topic, re.compile(pattern, _I))
    for topic, pattern in (
        (FaqTopic.SIGN_IN_LOCKOUT,
         r"\b(locked out|lockout|account (is |got |was )?locked|(sign[- ]?in|log[- ]?in|login) "
         r"(is |was )?(locked|blocked)|failed (sign[- ]?ins?|log[- ]?ins?|logins?|attempts))\b"),
        (FaqTopic.MFA,
         r"\b(mfa|2fa|two[- ]?(factor|step)|authenticator|totp|otp|recovery codes?"
         r"|verification codes?)\b"),
        (FaqTopic.FRAUD_HOLDS,
         r"\b(on hold|held|under review|being reviewed|pending review|fraud (checks?|holds?"
         r"|reviews?)|flagged)\b|\bwhy (was|is) (my )?(transfer|payment) (declined|blocked"
         r"|stopped)\b"),
        (FaqTopic.TRANSFER_LIMITS,
         r"\b(transfer|payment|sending|daily|per[- ]transfer|transaction) limits?\b"
         r"|\blimits? (on|for) (transfers?|payments?|sending)\b"
         r"|\bhow much can i (send|transfer|pay)\b|\bmaximum (transfer|payment)\b"),
        (FaqTopic.STATEMENTS,
         r"\b(download|export|request|print)\b.{0,30}\bstatements?\b"
         r"|\bstatements?\b.{0,30}\b(download|pdf|csv)\b|\b(pdf|csv) statements?\b"
         r"|\bhow (do|can) i\b.{0,30}\bstatements?\b"),
        (FaqTopic.SCHEDULED_TRANSFERS,
         r"\b(scheduled|recurring|repeating|automatic|standing) (transfers?|payments?|orders?)\b"
         r"|\bstanding orders?\b|\bschedule (a |an )?(transfer|payment)s?\b"),
        (FaqTopic.LOANS,
         r"\bloans?\b.{0,30}\b(work|rules|criteria|requirements?)\b"
         r"|\bhow (do|does|are)\b.{0,30}\b(loans?|emis?)\b.{0,20}\b(work|decided|approved"
         r"|repaid)\b"),
        (FaqTopic.FIXED_DEPOSITS,
         r"\b(fixed deposits?|fds?|term deposits?)\b.{0,40}\b(work|early|premature(ly)?|break"
         r"|clos(e|ing)|penalty|lock[- ]?in|compound(ed|ing)?|matur(e|es|ity))\b"),
    )
)  # fmt: skip
_PRODUCTS = re.compile(
    r"\b(what|which|list|show|tell me about)\b.{0,40}\b(products?|loans|deposits|fds|funds"
    r"|investments|bonds|schemes)\b.{0,30}\b(offer(ed)?|have|available|there)\b"
    r"|\b(products?|catalogue|catalog)\b"
    r"|\b(loan|fd|deposit|interest|fund|bond) rates?\b"
    r"|\brates? (on|for) (loans?|fds?|(fixed )?deposits?|funds?|bonds?)\b"
    r"|\bdo you (offer|have|sell)\b",
    _I,
)
_PRODUCT_CATEGORIES: tuple[tuple[ProductCategory, re.Pattern[str]], ...] = tuple(
    (category, re.compile(pattern, _I))
    for category, pattern in (
        (ProductCategory.LOANS, r"\b(loans?|emis?|borrow(ing)?)\b"),
        (ProductCategory.DEPOSITS, r"\b(fds?|(fixed |term )?deposits?)\b"),
        (ProductCategory.INVESTMENTS, r"\b(funds?|mutual|sips?|gold|bonds?|elss|invest\w*)\b"),
    )
)
_INVEST = re.compile(
    r"\b(suggest\w*|recommend\w*|advi[cs]e|ideas?|options?|should i|where (should|can|do) i"
    r"|what (should|can) i)\b.{0,40}\binvest\w*"
    r"|\binvest\w*\b.{0,30}\b(suggestions?|ideas?|options?|advice|recommendations?|for me)\b"
    r"|\b(grow|put|park) my (money|savings)\b",
    _I,
)
_PERSONAL = re.compile(r"\b(me|my|i)\b", _I)
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
    )
)


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
    tenure = parse_tenure(message) or DEFAULT_TENURE_MONTHS[loan_type]
    arguments: dict[str, object] = {"loan_type": loan_type.value, "tenure_months": tenure}
    amount = parse_rupees(message)
    if amount is not None:
        return Route(ToolName.LOAN_AFFORDABILITY, {**arguments, "amount_rupees": amount})
    if _LOAN_NAMED.search(message):
        return Route(ToolName.LOAN_AFFORDABILITY, arguments)  # FR-38: suggest, or ask why not
    return Route(None)


def parse_tenure(message: str) -> int | None:
    """Tenure in months the customer wrote ("5 years" -> 60, "18 months" -> 18), or None."""
    years, months = _YEARS.search(message), _MONTHS.search(message)
    if years:
        return int(years.group(1)) * 12
    return int(months.group(1)) if months else None


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


def _unusual(message: str) -> Route:
    period = SpendPeriod.LAST_MONTH if _LAST_MONTH.search(message) else SpendPeriod.THIS_MONTH
    return Route(ToolName.UNUSUAL_EXPENSES, _with_account(message, {"period": period.value}))


def faq_topics(message: str) -> list[FaqTopic]:
    return [topic for topic, rx in _FAQ if rx.search(message)]


def _category(message: str) -> ProductCategory:
    named = [category for category, rx in _PRODUCT_CATEGORIES if rx.search(message)]
    return named[0] if len(named) == 1 else ProductCategory.ALL


def _wants_suggestions(message: str) -> bool:
    """Personal investment questions; "what investment options do you offer" is the catalogue."""
    if not _INVEST.search(message):
        return False
    return not _PRODUCTS.search(message) or bool(_PERSONAL.search(message))


def _discovery(message: str) -> list[ToolName]:
    """TASK-105 intents: help topics, personal investment suggestions, the product catalogue."""
    found = [ToolName.FAQ_ANSWER] * len(faq_topics(message))  # two topics: unclear
    if _wants_suggestions(message):
        found.append(ToolName.INVESTMENT_SUGGESTIONS)
    elif _PRODUCTS.search(message):
        found.append(ToolName.LIST_PRODUCTS)
    return found


def _intents(message: str) -> list[ToolName]:
    """Every intent whose keywords appear, most specific first."""
    found = [
        tool
        for tool, rx in (
            (ToolName.BALANCE_FORECAST, _FORECAST),
            (ToolName.LOAN_AFFORDABILITY, _LOAN),
            (ToolName.UNUSUAL_EXPENSES, _UNUSUAL),
            (ToolName.RECENT_TRANSACTIONS, _TRANSACTIONS),
            (ToolName.SPEND, _SPEND),
        )
        if rx.search(message)
    ]
    # "unusual expenses" / "unexpected transactions" name ONE intent: the unusual-expense check
    # is the more specific reading of the spend and transaction keywords it contains.
    if ToolName.UNUSUAL_EXPENSES in found:
        found = [t for t in found if t not in (ToolName.SPEND, ToolName.RECENT_TRANSACTIONS)]
    discovery = _discovery(message)
    if discovery:
        # "how do loans work", "what loans do you offer", "download a statement", "transfer
        # limit": the help or catalogue reading is the specific one, unless an amount was named.
        dropped = {ToolName.RECENT_TRANSACTIONS}
        if parse_rupees(message) is None:
            dropped.add(ToolName.LOAN_AFFORDABILITY)
        found = [t for t in found if t not in dropped] + discovery
    # "account" appears in questions about anything ("spend on my second account"), so the
    # balance intent counts only when nothing more specific was named.
    if not found and _BALANCE.search(message):
        found.append(ToolName.ACCOUNT_SUMMARY)
    return found


def clear_route(message: str) -> Route | None:
    """The fast path: exactly one intent and its arguments parse. Otherwise None (ask the LLM).

    "spending and recent transactions" names two intents: unclear, so it goes to the model (or,
    without one, to :func:`route`). "Can I get a loan?" without an amount is clear since FR-38:
    the loan tool suggests the largest eligible amount or says why it cannot.
    """
    if len(_intents(message)) != 1 or _THIRD_PARTY.search(message):
        return None
    chosen = route(message)
    return chosen if chosen.tool is not None else None


def _discovery_route(message: str) -> Route | None:
    """Help, suggestions or catalogue - unless a loan amount makes it an affordability check."""
    if parse_rupees(message) is not None and _LOAN.search(message):
        return None
    topics = faq_topics(message)
    if topics:
        return Route(ToolName.FAQ_ANSWER, {"topic": topics[0].value})
    if _wants_suggestions(message):
        return Route(ToolName.INVESTMENT_SUGGESTIONS)
    if _PRODUCTS.search(message):
        return Route(ToolName.LIST_PRODUCTS, {"category": _category(message).value})
    return None


def route(message: str) -> Route:
    """Pick a tool by keywords. Order matters: the most specific intent wins."""
    if _FORECAST.search(message):
        return Route(
            ToolName.BALANCE_FORECAST, _with_account(message, {"horizon_days": _horizon(message)})
        )
    discovery = _discovery_route(message)
    if discovery is not None:
        return discovery
    if _LOAN.search(message):
        return _loan(message)
    if _UNUSUAL.search(message):
        return _unusual(message)
    if _TRANSACTIONS.search(message):
        return _transactions(message)
    if _SPEND.search(message):
        return _spend(message)
    if _BALANCE.search(message):
        return Route(ToolName.ACCOUNT_SUMMARY)
    return Route(None)
