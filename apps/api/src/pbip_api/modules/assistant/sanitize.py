"""Untrusted-text handling and the output filter for the assistant (TASK-059, Blueprint s10).

* The customer's message is normalised before anything inspects it: Unicode NFKC (so look-alike
  characters cannot hide a phrase from the guard), control, format and zero-width characters
  removed, whitespace collapsed, length capped at 500 characters.
* Retrieved banking text (merchant names, references, notes) is written by other people. It never
  reaches the LLM (the model only routes; ADR-0018). Where a reply shows it, it is delimited as
  quoted data, stripped of control characters and quotes, and capped.
* Every reply and every stored message passes :func:`redact_output`, which masks long digit runs
  (account and card numbers) to the last four digits, and replaces JWTs and email addresses.
* A message sent to an EXTERNAL model provider (Groq, ADR-0036) first passes
  :func:`redact_for_provider`: everything :func:`redact_output` removes, plus UUIDs (customer,
  user and account ids), Indian mobile numbers, and the signed-in customer's own name and email.
  The model only routes, so none of these change the tool it should choose.
"""

import re
import unicodedata
from collections.abc import Iterable

MAX_MESSAGE_CHARS = 500
MAX_UNTRUSTED_CHARS = 40
_WHITESPACE = re.compile(r"\s+")
_QUOTES = re.compile("[\"'`\u2018\u2019\u201c\u201d\u00ab\u00bb]")
# 12-19 digits, optionally grouped by single spaces or dashes (account/card numbers)
_LONG_NUMBER = re.compile(r"(?<![\d])(?:\d[ -]?){11,18}\d(?![\d])")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_UUID = re.compile(r"\b[0-9a-f]{8}-?(?:[0-9a-f]{4}-?){3}[0-9a-f]{12}\b", re.IGNORECASE)
# +91 / 0 prefix optional; ten digits starting 6-9 (TRAI mobile numbering)
_MOBILE = re.compile(r"(?<![\d])(?:\+?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?![\d])")
# Name parts shorter than this are not removed ("Al" would eat ordinary words).
_MIN_NAME_PART = 3


def _strip_controls(value: str) -> str:
    """Drop Unicode categories C* (controls, format chars incl. bidi overrides and zero-width)."""
    return "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in " \t\n")


def clean_user_message(value: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    normalised = unicodedata.normalize("NFKC", value)
    return _WHITESPACE.sub(" ", _strip_controls(normalised)).strip()[:limit]


def clean_untrusted_text(value: str | None, limit: int = MAX_UNTRUSTED_CHARS) -> str:
    """Drop control/format characters, collapse spaces, cap the length."""
    if not value:
        return ""
    collapsed = _WHITESPACE.sub(" ", _strip_controls(value)).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def untrusted_block(value: str | None) -> str:
    """Retrieved text as a quoted data block: it reads as a value, never as an instruction."""
    cleaned = _QUOTES.sub("", clean_untrusted_text(value))
    return f"“{cleaned}”" if cleaned else ""


def _mask_number(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "••" + digits[-4:]


def redact_output(value: str) -> str:
    """Mask account/card numbers to the last four digits; replace JWTs and emails."""
    value = _JWT.sub("[token removed]", value)
    value = _EMAIL.sub("[email removed]", value)
    return _LONG_NUMBER.sub(_mask_number, value)


def redact_for_provider(value: str, private_terms: Iterable[str] = ()) -> str:
    """Minimise what leaves the stack: the routing question without identifiers or PII.

    ``private_terms`` are the signed-in customer's own name parts and email, supplied by the
    server from the authenticated principal; each is replaced case-insensitively, whole words only.
    """
    value = _UUID.sub("[id removed]", value)
    value = _MOBILE.sub("[phone removed]", value)  # before redact_output masks +91... as a number
    value = redact_output(value)
    for term in sorted({t.strip() for t in private_terms if t}, key=len, reverse=True):
        if len(term) >= _MIN_NAME_PART:
            pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
            value = re.sub(pattern, "[name removed]", value, flags=re.IGNORECASE)
    return value
