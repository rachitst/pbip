"""Validation helpers shared by request schemas.

Why: fuzzing sent payee names containing NUL bytes and lone surrogates. Pydantic accepts them as
``str``, PostgreSQL cannot store them (NUL) or encode them (surrogates), and the request ended as
a 500 from the database driver instead of a 422 at the boundary.
"""

from typing import Annotated

from pydantic import AfterValidator


def storable_text(value: str) -> str:
    """Reject text that cannot be stored verbatim: control characters or unencodable code points."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        msg = "text contains characters that cannot be encoded"
        raise ValueError(msg) from exc
    if any(ch < " " or ch == "\x7f" for ch in value):
        msg = "text must not contain control characters"
        raise ValueError(msg)
    return value


def encodable_text(value: str) -> str:
    """Reject only what cannot be UTF-8 encoded (lone surrogates); control characters stay allowed.

    For secrets and opaque identifiers: a password may legitimately contain anything typeable, but
    a lone surrogate reaches the Argon2 hasher or ``.encode()`` and would end as a 500.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        msg = "text contains characters that cannot be encoded"
        raise ValueError(msg) from exc
    return value


Text = Annotated[str, AfterValidator(storable_text)]
"""Free-text request field (names, references, labels): plain ``str`` plus ``storable_text``."""

EncodableText = Annotated[str, AfterValidator(encodable_text)]
"""Passwords and opaque identifiers: plain ``str`` that is guaranteed UTF-8 encodable."""
