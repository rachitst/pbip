"""Offline common and breached password detection.

Design rationale and requirements (docs/PROJECT_BLUEPRINT.md & TASK-006):
1. Privacy and zero external exfiltration: In a regulated banking platform, customer
   passwords or k-anonymity hash prefixes MUST NOT be transmitted over the public internet
   to third-party lookup APIs (e.g. HaveIBeenPwned API), avoiding network dependencies,
   latency, third-party outages, or data leakage.
2. Deterministic & offline: Operates strictly in-memory with sub-millisecond latency.
3. Multi-layer heuristic defense:
   - Curated blocklist of hundreds of high-frequency breached root passwords, common corporate
     defaults, and cultural password terms.
   - Leetspeak and symbol de-obfuscation (e.g., '@' -> 'a', '0' -> 'o', '3' -> 'e', '$' -> 's').
   - Affix and delimiter stripping (digits, symbols, years appended to common roots).
   - QWERTY and numeric keyboard walk / sequence detection.
   - Predictable seasonal and calendar template detection (e.g., 'Summer2025!').

Limitations:
- Does not contain the entire multi-billion HIBP corpus (trade-off for zero network egress
  and zero memory footprint bloat).
- High-entropy multi-word passphrases (e.g. 'Correct-Horse-9!') are intentionally permitted
  in alignment with NIST SP 800-63B guidelines.
"""

from __future__ import annotations

import re
from typing import Final

from pbip_api.core.errors import UnprocessableError

# Common breached roots, high-frequency default passwords, and dictionary bases.
# Sourced from top entries in SecLists / RockYou / NCSC / NordPass breached password corpuses.
_BREACHED_ROOT_WORDS: Final[frozenset[str]] = frozenset(
    {
        # Common credentials & defaults
        "password",
        "passphrase",
        "passcode",
        "secret",
        "default",
        "welcome",
        "admin",
        "administrator",
        "login",
        "changeme",
        "letmein",
        "access",
        "account",
        "master",
        "system",
        "security",
        "root",
        "super",
        "superuser",
        "operator",
        "guest",
        "banking",
        "banker",
        "retail",
        "portal",
        "support",
        "service",
        "testing",
        "testpass",
        "test1234",
        "company",
        "corporation",
        "finance",
        "money",
        "database",
        "server",
        "client",
        "network",
        "manager",
        "employee",
        "user1234",
        "newpassword",
        "trustno1",
        "standard",
        "internal",
        "temporary",
        "temp1234",
        "customer",
        "internet",
        "desktop",
        "computer",
        # Common phrases & expressions
        "iloveyou",
        "loveme",
        "whatever",
        "freedom",
        "sunshine",
        "princess",
        "killer",
        "cheese",
        "coffee",
        "cookie",
        "diamond",
        "galaxy",
        "guitar",
        "hunter",
        "pepper",
        "phoenix",
        "ranger",
        "spider",
        "tiger",
        "wizard",
        "yellow",
        "zipper",
        "shadow",
        "mustang",
        "starwars",
        "pokemon",
        "batman",
        "superman",
        "matrix",
        "dragon",
        "monkey",
        # Common sports & clubs
        "football",
        "baseball",
        "basketball",
        "soccer",
        "hockey",
        "tennis",
        "golfing",
        "swimming",
        "running",
        "cricket",
        "rugby",
        "badminton",
        "volleyball",
        "olympics",
        "liverpool",
        "arsenal",
        "chelsea",
        "barcelona",
        "realmadrid",
        "manchester",
        "juventus",
        "bayern",
        # Brands & automobiles
        "ferrari",
        "porsche",
        "corvette",
        "camaro",
        "mercedes",
        "harley",
        "ducati",
        "kawasaki",
        "yamaha",
        "suzuki",
        "honda",
        "toyota",
        "nissan",
        "volvo",
        "audi",
        "bmw",
        # Common given names frequently appearing in weak passwords
        "michael",
        "jessica",
        "daniel",
        "thomas",
        "charlie",
        "jordan",
        "andrew",
        "joshua",
        "william",
        "anthony",
        "ashley",
        "brandon",
        "joseph",
        "matthew",
        "robert",
        "david",
        "james",
        "john",
        "donald",
        "richard",
        "george",
        "steven",
        "edward",
        "brian",
        "ronald",
        "kevin",
        "jason",
        "jeffrey",
        "frank",
        "gary",
        "stephen",
        "eric",
        "larry",
        "justin",
        "scott",
        "samuel",
        "gregory",
        "alexander",
        "patrick",
        "jack",
        "dennis",
        "jerry",
        "tyler",
        "aaron",
        "henry",
        "douglas",
        "peter",
        "adam",
        "nathan",
        "zachary",
        "walter",
        "kyle",
        "harold",
        "carl",
        "jeremy",
        "keith",
        "roger",
        "gerald",
        "ethan",
        "arthur",
        "terry",
        "christian",
        "sean",
        "lawrence",
        "austin",
        "joe",
        "noah",
        "jesse",
        "albert",
        "bryan",
        "billy",
        "bruce",
        "willie",
        "dylan",
        "alan",
        "ralph",
        "gabriel",
        "roy",
        "juan",
        "logan",
        "randy",
        "louis",
        "russell",
        "vincent",
        "philip",
        "bobby",
        "johnny",
        "bradley",
        "emma",
        "olivia",
        "sophia",
        "isabella",
        "mia",
        "charlotte",
        "amelia",
        "harper",
        "evelyn",
        "abigail",
        "emily",
        "elizabeth",
        "mila",
        "ella",
        "avery",
        "sofia",
        "camila",
        "aria",
        "scarlett",
        "victoria",
        "madison",
        "luna",
        "grace",
        "chloe",
        "penelope",
        "layla",
        "riley",
        "zoey",
        "nora",
        "lily",
        "eleanor",
        "hannah",
        "lillian",
        "addison",
        "aubrey",
        "ellie",
        "stella",
        "natalie",
        "zoe",
        "leah",
        "hazel",
        "violet",
        "aurora",
        "savannah",
        "audrey",
        "brooklyn",
        "bella",
        "claire",
        "skylar",
        "lucy",
        "paisley",
        "everly",
        "anna",
        "caroline",
        "nova",
        "genesis",
        "emilia",
        "kennedy",
        "samantha",
        "maya",
        "willow",
        "kinsley",
        "naomi",
        "aaliyah",
        "elena",
        "sarah",
        "ariana",
        "allison",
        "gabriella",
        "alice",
        "madelyn",
        "cora",
        "ruby",
        "eva",
        "serenity",
        "autumn",
        "adeline",
        "hailey",
        "gianna",
        "valentina",
        "isla",
        "eliana",
        "quinn",
        "nevaeh",
        "ivy",
        "sadie",
        "piper",
        "lydia",
        "alexa",
        "josephine",
        "emery",
        "julia",
        "delilah",
        "arianna",
        "vivian",
        "kaylee",
        "sophie",
        "brielle",
        "madeline",
        "peyton",
        "rylee",
        "clara",
        "hadley",
        "melanie",
        "mackenzie",
        "reagan",
        "adaline",
        "aubree",
        "mary",
    }
)

_LEET_TRANSLATION: Final[dict[int, str]] = str.maketrans(
    {
        "@": "a",
        "4": "a",
        "8": "b",
        "(": "c",
        "3": "e",
        "9": "g",
        "0": "o",
        "$": "s",
        "5": "s",
        "7": "t",
        "+": "t",
    }
)

_TRAILING_AFFIX_RE: Final[re.Pattern[str]] = re.compile(r"(?:[\W_]*\d{2,}[\W_]*|[\W_]+|\d+[\W_]*)$")
_LEADING_AFFIX_RE: Final[re.Pattern[str]] = re.compile(r"^(?:[\W_]*\d{2,}[\W_]*|[\W_]+|^[\W_]*\d+)")
_EMBEDDED_I_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z])[1!|](?=[a-z])")

_KEYBOARD_SEQUENCES: Final[tuple[str, ...]] = (
    "1234567890",
    "0987654321",
    "qwertyuiop",
    "poiuytrewq",
    "asdfghjkl",
    "lkjhgfdsa",
    "zxcvbnm",
    "mnbvcxz",
    "qazwsxedc",
)

_SEASONAL_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:spring|summer|autumn|fall|winter|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\d{2,4}",
    re.IGNORECASE,
)


def normalize_password_core(password: str) -> str:
    """De-obfuscate leetspeak, strip non-alpha borders, and collapse repeated characters."""
    # 1. Strip outer multi-digit numbers (years/counters) and boundary symbols
    trimmed = _LEADING_AFFIX_RE.sub("", password)
    trimmed = _TRAILING_AFFIX_RE.sub("", trimmed)
    lowered = trimmed.lower()
    # 2. Translate high-confidence symbol-to-letter leetspeak substitutions
    translated = lowered.translate(_LEET_TRANSLATION)
    # 3. De-obfuscate embedded 1/!/| acting as 'i' or 'l' between letters (e.g. 'adm1n' -> 'admin')
    with_i = _EMBEDDED_I_RE.sub("i", translated)
    # 4. Strip any remaining non-letter border characters
    stripped = re.sub(r"^[^a-z]+|[^a-z]+$", "", with_i)
    # 5. Collapse 3 or more consecutive identical characters into two
    collapsed = re.sub(r"(.)\1{2,}", r"\1\1", stripped)
    return collapsed


def has_keyboard_sequence(password: str, min_length: int = 5) -> bool:
    """Check for keyboard sequences of at least `min_length` characters."""
    lowered = password.lower()
    for seq in _KEYBOARD_SEQUENCES:
        for i in range(len(seq) - min_length + 1):
            sub = seq[i : i + min_length]
            if sub in lowered:
                return True
    return False


def is_seasonal_pattern(password: str) -> bool:
    """Detect calendar/seasonal templates like 'Summer2024!' or 'January2025#'."""
    return bool(_SEASONAL_RE.match(password))


def check_common_or_breached(password: str) -> str | None:
    """Return a reason if password matches common/breached criteria, else None."""
    # 1. Check predictable calendar/seasonal pattern
    if is_seasonal_pattern(password):
        return "password matches a predictable seasonal or calendar pattern"

    # 2. Check keyboard walk or sequential pattern
    if has_keyboard_sequence(password):
        return "password contains a predictable keyboard sequence"

    # 3. Check exact lower-case match against breached root
    lowered = password.lower()
    if lowered in _BREACHED_ROOT_WORDS:
        return "password is too common or appears in known data breaches"

    # 4. Check core word after leetspeak de-obfuscation and affix stripping
    core = normalize_password_core(password)
    if core and core in _BREACHED_ROOT_WORDS:
        return "password is too common or appears in known data breaches"

    return None


def validate_not_breached_or_common(password: str) -> None:
    """Raise UnprocessableError if password matches common or breached criteria."""
    reason = check_common_or_breached(password)
    if reason is not None:
        raise UnprocessableError(reason)
