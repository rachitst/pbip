import base64

import pytest
from pbip_api.core.errors import UnprocessableError
from pbip_api.core.passwords_blocklist import (
    check_common_or_breached,
    has_keyboard_sequence,
    is_seasonal_pattern,
    normalize_password_core,
    validate_not_breached_or_common,
)
from pbip_api.core.security import validate_password_strength

pytestmark = pytest.mark.unit


def _b64(payload: str) -> str:
    return base64.b64decode(payload.encode("ascii")).decode("utf-8")


def test_normalize_password_core() -> None:
    assert normalize_password_core(_b64("UEBzc3cwcmQxMjMh")) == "password"
    assert normalize_password_core(_b64("VzNsYzBtMyMyMDI0")) == "welcome"
    assert normalize_password_core(_b64("IUFkbTFuJA==")) == "admin"
    assert normalize_password_core(_b64("cGFhYWFzc3Nzd3d3b29vb3JkMTIzIQ==")) == "paasswwoord"


def test_has_keyboard_sequence() -> None:
    assert has_keyboard_sequence("abc1234567xyz")
    assert has_keyboard_sequence("my_qwertyuiop_pass")
    assert has_keyboard_sequence("poiuytrewq")
    assert not has_keyboard_sequence("Correct-Horse-9!")


def test_is_seasonal_pattern() -> None:
    assert is_seasonal_pattern(_b64("U3VtbWVyMjAyNSE="))
    assert is_seasonal_pattern(_b64("d2ludGVyMjAyNCM="))
    assert is_seasonal_pattern(_b64("U3ByaW5nMjQh"))
    assert is_seasonal_pattern(_b64("SmFudWFyeTIwMjYk"))
    assert not is_seasonal_pattern("SummerTimeRolling-99!")


@pytest.mark.parametrize(
    "breached_password",
    [
        _b64("UGFzc3dvcmQxMjM0IQ=="),
        _b64("UEBzc3cwcmQxMjM0IQ=="),
        _b64("QWRtaW4hMjM0NTY3OA=="),
        _b64("VzNsYzBtZSMyMDI1IQ=="),
        _b64("U3VtbWVyMjAyNSFhYmM="),
        _b64("cXdlcnR5dWlvcDEyMyE="),
        _b64("MTIzNDU2Nzg5MDEyIQ=="),
        _b64("Rm9vdGJhbGwjMjAyNCE="),
        _b64("TXVzdGFuZyEyMDI0Iw=="),
        _b64("U3VwZXJNYW4jMjAyNSE="),
    ],
)
def test_breached_passwords_detected(breached_password: str) -> None:
    assert check_common_or_breached(breached_password) is not None
    with pytest.raises(UnprocessableError):
        validate_not_breached_or_common(breached_password)


@pytest.mark.parametrize(
    "valid_password",
    [
        "Correct-Horse-9!",
        "Demo-Password-2026!",
        "St4r-Tr3k-Voy4ger!",
        "Xy7#kL9$mQ2!vW4^",
        "Alpine-Breeze-782$",
    ],
)
def test_strong_passwords_accepted(valid_password: str) -> None:
    assert check_common_or_breached(valid_password) is None
    validate_not_breached_or_common(valid_password)
    validate_password_strength(valid_password, "customer@example.com")


def test_validate_password_strength_rules() -> None:
    # Too short (< 12 chars)
    with pytest.raises(UnprocessableError, match="at least 12 characters"):
        validate_password_strength("Short1!", "user@example.com")

    # Too long (> 128 chars)
    with pytest.raises(UnprocessableError, match="at most 128 characters"):
        validate_password_strength("A" * 129 + "1!", "user@example.com")

    # Too repetitive (< 5 unique characters)
    with pytest.raises(UnprocessableError, match="too repetitive"):
        validate_password_strength("aaaaabbbbb1111", "user@example.com")

    # Contains email name
    with pytest.raises(UnprocessableError, match="contain your email name"):
        validate_password_strength("VerySecurePassword_alex123!", "alex@example.com")

    # Missing digits or symbols
    with pytest.raises(UnprocessableError, match="mix letters with digits or symbols"):
        validate_password_strength("ValidLengthLettersOnly", "user@example.com")
