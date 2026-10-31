"""Untrusted-text handling and the output filter for the assistant (TASK-059, Blueprint s10).

* The customer's message is normalised before anything inspects it: Unicode NFKC (so look-alike
  characters cannot hide a phrase from the guard), control, format and zero-width characters
  removed, whitespace collapsed, length capped at 500 characters.
* Retrieved banking text (merchant names, references, notes) is written by other people. It never
  reaches the LLM (the model only routes; ADR-0018). Where a reply shows it, it is delimited as
  quoted data, stripped of control characters and quotes, and capped.
* Every reply and every stored message passes :func:`redact_output`, which masks long digit runs
  (account and card numbers) to the last four digits, and replaces JWTs and email addresses.
"""

import re
import unicodedata

MAX_MESSAGE_CHARS = 500
MAX_UNTRUSTED_CHARS = 40
_WHITESPACE = re.compile(r"\s+")
_QUOTES = re.compile("[\"'`\u2018\u2019\u201c\u201d\u00ab\u00bb]")
# 12-19 digits, optionally grouped by single spaces or dashes (account/card numbers)
_LONG_NUMBER = re.compile(r"(?<![\d])(?:\d[ -]?){11,18}\d(?![\d])")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


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
