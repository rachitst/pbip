"""Free-text request fields reject what PostgreSQL cannot store (fuzzing finding, 500 -> 422)."""

import pytest
from pbip_api.core.validation import encodable_text, storable_text
from pbip_api.modules.auth.schemas import LoginRequest, PasswordChangeRequest, RegisterRequest
from pbip_api.modules.bills.schemas import PayBillRequest
from pbip_api.modules.customers.schemas import DeviceRegisterRequest
from pbip_api.modules.payees.schemas import PayeeCreate
from pbip_api.modules.payments.schemas import TransferRequest
from pbip_test_support import PASSWORD
from pydantic import ValidationError

pytestmark = pytest.mark.unit

BAD = ["with\x00nul", "tab\tinside", "new\nline", "del\x7f", "lone\ud800surrogate"]


@pytest.mark.parametrize("value", BAD)
def test_storable_text_rejects_control_characters_and_unencodable_text(value: str) -> None:
    with pytest.raises(ValueError, match=r"control characters|cannot be encoded"):
        storable_text(value)


def test_storable_text_keeps_ordinary_unicode() -> None:
    assert storable_text("Ravi \u0915\u0941\u092e\u093e\u0930 O'Brien-Smith \u00e9") == (
        "Ravi \u0915\u0941\u092e\u093e\u0930 O'Brien-Smith \u00e9"
    )


@pytest.mark.parametrize("value", BAD)
def test_payee_name_and_nickname_are_validated(value: str) -> None:
    with pytest.raises(ValidationError):
        PayeeCreate(name=value, account_number="PBIP123456")
    with pytest.raises(ValidationError):
        PayeeCreate(name="Alice", account_number="PBIP123456", nickname=value)


def test_registration_name_and_transfer_reference_are_validated() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(email="a@example.com", password=PASSWORD, full_name="A\x00B")
    with pytest.raises(ValidationError):
        TransferRequest.model_validate(
            {
                "from_account_id": "00000000-0000-0000-0000-000000000000",
                "amount_minor": 1,
                "payee_id": "00000000-0000-0000-0000-000000000000",
                "reference": "x\ud800",
            }
        )
    with pytest.raises(ValidationError):
        PayBillRequest.model_validate(
            {
                "from_account_id": "00000000-0000-0000-0000-000000000000",
                "biller_id": "00000000-0000-0000-0000-0000000000b1",
                "consumer_reference": "CA\x00123",
                "amount_minor": 100,
            }
        )


def test_encodable_text_rejects_only_unencodable_text() -> None:
    """Passwords may contain tabs or control characters; only lone surrogates are refused."""
    assert (
        encodable_text("tab\tand\x00nul are fine in a secret")
        == "tab\tand\x00nul are fine in a secret"
    )
    with pytest.raises(ValueError, match="cannot be encoded"):
        encodable_text("lone\ud800surrogate")


def test_password_fields_and_device_fingerprint_reject_lone_surrogates() -> None:
    """Regression (audit H8): a lone surrogate reached Argon2 / .encode() and produced a 500."""
    bad = PASSWORD + "\ud800"  # a valid password made unencodable by a lone surrogate
    with pytest.raises(ValidationError):
        LoginRequest(email="a@example.com", password=bad)
    with pytest.raises(ValidationError):
        RegisterRequest(email="a@example.com", password=bad, full_name="A B")
    with pytest.raises(ValidationError):
        PasswordChangeRequest(current_password=PASSWORD, new_password=bad)
    with pytest.raises(ValidationError):
        PasswordChangeRequest(current_password=bad, new_password=PASSWORD)
    with pytest.raises(ValidationError):
        DeviceRegisterRequest(fingerprint="\ud800")
    with_controls = PASSWORD + "\x00\t"  # control characters are allowed in a secret
    assert LoginRequest(email="a@example.com", password=with_controls).password == with_controls
