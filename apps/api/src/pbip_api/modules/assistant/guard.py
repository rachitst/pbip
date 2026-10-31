"""Deterministic refusals that run BEFORE any model sees the message (Blueprint section 10).

These do not depend on the LLM behaving. They catch the requests the assistant must never act
on: moving money, naming another person's data, SQL, and attempts to rewrite its instructions.
The tool registry is the real boundary (read-only, no identifiers); this layer gives the
customer a clear answer and an audit record instead of a confused tool call.
"""

import base64
import binascii
import re
from enum import StrEnum


class Refusal(StrEnum):
    MONEY_MOVEMENT = "money_movement"
    OTHER_CUSTOMER = "other_customer"
    SQL = "sql"
    INSTRUCTION_OVERRIDE = "instruction_override"
    PRIVILEGE = "privilege"


REFUSAL_TEXT: dict[Refusal, str] = {
    Refusal.MONEY_MOVEMENT: (
        "I can't move money. Use Transfers or Bill payments, which confirm the details and "
        "may ask for verification."
    ),
    Refusal.OTHER_CUSTOMER: "I can only help with your own accounts.",
    Refusal.SQL: "I can't run database queries. Ask me about your balance, spending or loans.",
    Refusal.INSTRUCTION_OVERRIDE: (
        "I can't change how I work. I can help with your balance, transactions, spending, "
        "balance forecast and loan affordability."
    ),
    Refusal.PRIVILEGE: "I can't change roles or permissions. I can only read your own accounts.",
}

REFUSAL_LINK: dict[Refusal, str | None] = {
    Refusal.MONEY_MOVEMENT: "/transfers",
    Refusal.OTHER_CUSTOMER: None,
    Refusal.SQL: None,
    Refusal.INSTRUCTION_OVERRIDE: None,
    Refusal.PRIVILEGE: None,
}

_I = re.IGNORECASE
_MONEY = re.compile(
    r"\b(send|transfer|wire|remit|move|withdraw|pay)\b(?!\s+(history|limit|limits|fee|fees)\b)"
    r"(?=.*(\d|₹|\brs\b|\binr\b|\brupees?\b|\blakh|\bcrore|\bmoney\b|\bfunds?\b|\bcash\b"
    r"|\bto\b|\bbill\b|\bpayee\b|\ball\b))",
    _I,
)
_PAST_TENSE_SPEND = re.compile(
    r"\b(did|have)\s+i\s+(send|sent|pay|paid|transfer|transferred|spend|spent)\b"
    r"|\bi'?ve\s+(sent|paid|transferred|spent)\b",
    _I,
)
_OTHER = re.compile(
    r"\b(another|other|someone|somebody|different)\s*(else'?s?|customer|user|person|people)"
    r"|\b(customer|user|client|account)[\s_-]*(id|number|no)\b"
    r"|\b(?!my\b|your\b|our\b)[a-z]+'s\s+(balance|account|accounts|transactions|spending|loan)",
    _I,
)
_SQL = re.compile(
    r"\b(select|insert|update|delete|drop|alter|truncate|grant)\b.{0,80}"
    r"\b(from|into|table|set|where|database|privileges)\b|;\s*--|\bunion\s+select\b",
    _I,
)
_OVERRIDE = re.compile(
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instruction|instructions|rules|"
    r"prompt|guardrails?|polic(y|ies))\b"
    r"|\b(system|developer)\s+(prompt|message|mode)\b|\byou\s+are\s+now\b|\bjailbreak\b",
    _I,
)


_PRIVILEGE = re.compile(
    r"\b(admin|administrator|root|superuser|staff|analyst|compliance)\s+(rights|access|"
    r"privileges?|role|mode|panel|permissions?)\b"
    r"|\b(make|grant|give|promote)\s+me\b.{0,30}\b(admin|staff|analyst|superuser|root)\b"
    r"|\b(credentials?|passwords?|api[\s_-]?keys?|secrets?|tokens?)\b.{0,30}"
    r"\b(database|db|server|system|admin)\b"
    r"|\b(database|db|server|system|admin)\b.{0,30}\b(credentials?|passwords?|secrets?)\b",
    _I,
)
_BASE64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def _decoded_variants(message: str) -> list[str]:
    """Base64 runs decoded to text, so an encoded instruction meets the same checks."""
    out = []
    for token in _BASE64.findall(message):
        try:
            text = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            continue
        if text.isprintable():
            out.append(text)
    return out


def check(message: str) -> Refusal | None:
    """Return why this message (or a base64 payload inside it) must be refused, or ``None``."""
    for variant in (message, *_decoded_variants(message)):
        refusal = _check_one(variant)
        if refusal is not None:
            return refusal
    return None


def _check_one(message: str) -> Refusal | None:
    if _OVERRIDE.search(message):
        return Refusal.INSTRUCTION_OVERRIDE
    if _PRIVILEGE.search(message):
        return Refusal.PRIVILEGE
    if _SQL.search(message):
        return Refusal.SQL
    if _OTHER.search(message):
        return Refusal.OTHER_CUSTOMER
    if _MONEY.search(message) and not _PAST_TENSE_SPEND.search(message):
        return Refusal.MONEY_MOVEMENT
    return None
