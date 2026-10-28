"""scripts/rotate_field_key.py against real rows: the runbook's key rotation (section 3.2)."""

from collections.abc import Callable

import pytest
from cryptography.fernet import Fernet
from pbip_api.core.config import Settings
from pbip_api.core.crypto import CryptoError, FieldCipher
from pbip_api.modules.auth.models import RoleName, User
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.customers.models import Customer
from pbip_test_support import PASSWORD
from sqlalchemy import select
from sqlalchemy.orm import Session

from scripts.rotate_field_key import rotate_rows

pytestmark = pytest.mark.integration

PHONE = "+91 98765 43210"


def _seed(db: Session, settings: Settings, factory: Callable[[Session], AuthService]) -> str:
    """A staff user with an encrypted TOTP secret and a customer with an encrypted phone, both under
    the current (old) key. Returns the TOTP secret so the test can prove decryption still works."""
    service = factory(db)
    staff = service.create_user(
        "rotate@demo.pbip", PASSWORD, "Rotate", [RoleName.FRAUD_ANALYST], RequestMeta()
    )
    secret = service.begin_mfa_enrollment(staff.id).secret
    service.register_customer("holder@demo.pbip", PASSWORD, "Holder", RequestMeta())
    customer = db.execute(select(Customer)).scalar_one()
    customer.phone_enc = FieldCipher(settings.field_encryption_key.get_secret_value()).encrypt(
        PHONE
    )
    db.commit()
    return secret


def _snapshot(db: Session) -> dict[str, str]:
    """The stored ciphertexts, one per encrypted column (the fixtures seed exactly one row each)."""
    user_token = db.execute(
        select(User.mfa_secret_enc).where(User.mfa_secret_enc.is_not(None))
    ).scalar_one()
    phone_token = db.execute(
        select(Customer.phone_enc).where(Customer.phone_enc.is_not(None))
    ).scalar_one()
    return {"users.mfa_secret_enc": user_token, "customers.phone_enc": phone_token}


def test_dry_run_changes_nothing_and_apply_re_encrypts_under_the_new_key(
    db: Session, settings: Settings, auth_service_factory: Callable[[Session], AuthService]
) -> None:
    old_key = settings.field_encryption_key.get_secret_value()
    secret = _seed(db, settings, auth_service_factory)
    before = _snapshot(db)
    new_key = Fernet.generate_key().decode()
    rotating = FieldCipher(f"{new_key},{old_key}")  # new key first, old key still listed

    counts = rotate_rows(db, rotating, apply=False)
    db.expire_all()
    assert counts == {"users.mfa_secret_enc": 1, "customers.phone_enc": 1}
    assert _snapshot(db) == before  # a dry run writes nothing

    assert rotate_rows(db, rotating, apply=True) == counts
    db.expire_all()
    after = _snapshot(db)
    assert after != before
    new_only, old_only = FieldCipher(new_key), FieldCipher(old_key)
    assert new_only.decrypt(after["users.mfa_secret_enc"]) == secret
    assert new_only.decrypt(after["customers.phone_enc"]) == PHONE
    for value in after.values():
        with pytest.raises(CryptoError):
            old_only.decrypt(value)  # the old key can now be retired


def test_rotation_aborts_without_writing_when_the_old_key_is_missing(
    db: Session, settings: Settings, auth_service_factory: Callable[[Session], AuthService]
) -> None:
    _seed(db, settings, auth_service_factory)
    before = _snapshot(db)
    unrelated = FieldCipher(Fernet.generate_key().decode())  # the old key was dropped too early

    with pytest.raises(CryptoError):
        rotate_rows(db, unrelated, apply=True)
    db.rollback()
    db.expire_all()
    assert _snapshot(db) == before
