import pytest
from cryptography.fernet import Fernet
from pbip_api.core.crypto import CryptoError, FieldCipher

pytestmark = pytest.mark.unit


def test_round_trip() -> None:
    cipher = FieldCipher(Fernet.generate_key().decode())
    token = cipher.encrypt("+91 98765 43210")
    assert token != "+91 98765 43210"
    assert cipher.decrypt(token) == "+91 98765 43210"


def test_tampered_ciphertext_fails_authentication() -> None:
    cipher = FieldCipher(Fernet.generate_key().decode())
    token = cipher.encrypt("secret")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(CryptoError):
        cipher.decrypt(tampered)


def test_key_rotation_new_key_first_old_key_still_decrypts() -> None:
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    token = FieldCipher(old_key).encrypt("legacy")
    rotated = FieldCipher(f"{new_key},{old_key}")
    assert rotated.decrypt(token) == "legacy"
    assert FieldCipher(new_key).decrypt(rotated.encrypt("fresh")) == "fresh"


@pytest.mark.parametrize("bad", ["", "not-a-valid-key"])
def test_invalid_keys_are_rejected(bad: str) -> None:
    with pytest.raises(CryptoError):
        FieldCipher(bad)
