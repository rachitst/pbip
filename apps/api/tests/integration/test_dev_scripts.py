"""Local dev scripts keep secrets out of files and logs (CodeQL py/clear-text-storage and
py/clear-text-logging on scripts/seed_dev.py): MFA secrets stay encrypted in the database and
scripts/totp.py derives codes from there."""

from collections.abc import Callable
from pathlib import Path

import pyotp
import pytest
from pbip_api.core.config import Settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.modules.auth.models import RoleName, User
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_test_support import PASSWORD, FakeClock, totp_code
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from scripts import seed_dev, totp

pytestmark = pytest.mark.integration


def _cipher(settings: Settings) -> FieldCipher:
    return FieldCipher(settings.field_encryption_key.get_secret_value())


def test_totp_code_is_derived_from_the_encrypted_database_secret(
    db: Session,
    settings: Settings,
    clock: FakeClock,
    auth_service_factory: Callable[[Session], AuthService],
) -> None:
    service = auth_service_factory(db)
    user = service.create_user(
        "analyst@demo.pbip", PASSWORD, "Analyst", [RoleName.FRAUD_ANALYST], RequestMeta()
    )
    secret = service.begin_mfa_enrollment(user.id).secret
    service.confirm_mfa_enrollment(user.id, totp_code(secret, clock), RequestMeta())

    code = totp.current_code(db, _cipher(settings), "  Analyst@Demo.PBIP ", clock.now)

    assert code == totp_code(secret, clock)


def test_totp_returns_none_for_unknown_or_unenrolled_users(
    db: Session, settings: Settings, auth_service_factory: Callable[[Session], AuthService]
) -> None:
    service = auth_service_factory(db)
    user = service.create_user(
        "pending@demo.pbip", PASSWORD, "Pending", [RoleName.FRAUD_ANALYST], RequestMeta()
    )
    service.begin_mfa_enrollment(user.id)  # secret stored, enrollment never confirmed

    assert totp.current_code(db, _cipher(settings), "pending@demo.pbip", 0) is None
    assert totp.current_code(db, _cipher(settings), "nobody@demo.pbip", 0) is None


def test_seed_writes_no_secret_file_and_prints_no_secrets(
    migrated_engine: Engine,
    db: Session,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(seed_dev, "get_settings", lambda: settings)
    monkeypatch.setattr(seed_dev, "get_engine", lambda: migrated_engine)
    monkeypatch.chdir(tmp_path)  # the old script wrote .dev-secrets.txt into the working directory

    assert seed_dev.main() == 0

    output = capsys.readouterr()
    assert list(tmp_path.iterdir()) == []
    assert seed_dev.DEMO_PASSWORD not in output.out + output.err
    staff = db.scalars(select(User).where(User.mfa_enabled.is_(True))).all()
    assert {user.email for user in staff} == {email for email, _, _ in seed_dev.STAFF}
    for user in staff:
        assert user.mfa_secret_enc is not None
        secret = _cipher(settings).decrypt(user.mfa_secret_enc)
        assert secret not in output.out + output.err
        assert secret not in user.mfa_secret_enc  # stored encrypted, not in plaintext
        # The seeded staff can still log in: totp.py derives the same code from the database.
        assert totp.current_code(db, _cipher(settings), user.email, 1_000_000) == pyotp.TOTP(
            secret
        ).at(1_000_000)
