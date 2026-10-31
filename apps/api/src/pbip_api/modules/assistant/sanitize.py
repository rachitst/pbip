"""Untrusted-text handling for the assistant (Blueprint section 10, rule 5).

Merchant names, references and notes come from other people. They are shown to the customer
only after control and formatting characters are removed and the length is capped. They are
never sent to the LLM: the model chooses a tool, and the server writes the answer.
"""

import re
import unicodedata

MAX_UNTRUSTED_CHARS = 40
_WHITESPACE = re.compile(r"\s+")


def clean_untrusted_text(value: str | None, limit: int = MAX_UNTRUSTED_CHARS) -> str:
    """Drop control/format characters (incl. bidi overrides), collapse spaces, cap the length."""
    if not value:
        return ""
    kept = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in " \t\n")
    collapsed = _WHITESPACE.sub(" ", kept).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"
