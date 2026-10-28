"""Key rotation primitives used by scripts/rotate_field_key.py (runbook section 3.2)."""

from types import SimpleNamespace
from typing import NoReturn

import pytest
from cryptography.fernet import Fernet
from pbip_api.core.crypto import CryptoError, FieldCipher
from pydantic import SecretStr

from scripts import rotate_field_key

pytestmark = pytest.mark.unit


def test_rotate_re_encrypts_under_the_primary_key_only() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token_under_old = FieldCipher(old).encrypt("JBSWY3DPEHPK3PXP")

    rotated = FieldCipher(f"{new},{old}").rotate(token_under_old)

    assert FieldCipher(new).decrypt(rotated) == "JBSWY3DPEHPK3PXP"
    with pytest.raises(CryptoError):
        FieldCipher(old).decrypt(rotated)


def test_rotate_refuses_a_token_no_configured_key_can_decrypt() -> None:
    token = FieldCipher(Fernet.generate_key().decode()).encrypt("x")
    with pytest.raises(CryptoError):
        FieldCipher(Fernet.generate_key().decode()).rotate(token)


def _no_database() -> NoReturn:
    raise AssertionError("must not connect to a database")


def test_main_exits_1_without_a_key_and_never_touches_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rotate_field_key,
        "get_settings",
        lambda: SimpleNamespace(field_encryption_key=SecretStr("")),
    )
    monkeypatch.setattr(rotate_field_key, "get_engine", _no_database)

    assert rotate_field_key.main(["rotate_field_key.py", "--apply"]) == 1


def test_main_reports_counts_and_key_count_without_printing_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = f"{Fernet.generate_key().decode()},{Fernet.generate_key().decode()}"
    monkeypatch.setattr(
        rotate_field_key,
        "get_settings",
        lambda: SimpleNamespace(field_encryption_key=SecretStr(keys)),
    )
    monkeypatch.setattr(rotate_field_key, "get_engine", lambda: None)
    seen: dict[str, bool] = {}

    def fake_rotate(session: object, cipher: FieldCipher, *, apply: bool) -> dict[str, int]:
        seen["apply"] = apply
        return {"users.mfa_secret_enc": 3, "customers.phone_enc": 0}

    monkeypatch.setattr(rotate_field_key, "rotate_rows", fake_rotate)

    assert rotate_field_key.main(["rotate_field_key.py"]) == 0
    out = capsys.readouterr().out
    assert seen == {"apply": False}
    assert "would re-encrypt (dry run) 3 row(s) in users.mfa_secret_enc" in out
    assert "keys configured: 2" in out
    for key in keys.split(","):
        assert key not in out
