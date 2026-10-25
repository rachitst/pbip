"""Print the current TOTP code for a seeded demo staff user (LOCAL database only).

    uv run python scripts/totp.py analyst@demo.pbip

The MFA secret is read from the local database and decrypted with the app's FieldCipher (the same
FIELD_ENCRYPTION_KEY the API uses), so no plaintext copy of a secret is ever written to disk.
Refuses to run against staging/production, like the seed script.
"""

import sys
import time

import pyotp
from pbip_api.core.config import get_settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.db import get_engine
from pbip_api.modules.auth.models import User
from pbip_api.modules.auth.service import normalize_email
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


def current_code(session: Session, cipher: FieldCipher, email: str, now: float) -> str | None:
    """The TOTP code for an MFA-enrolled user at ``now``, or None if there is no such user."""
    secret_enc = session.scalar(
        select(User.mfa_secret_enc).where(
            User.email == normalize_email(email), User.mfa_enabled.is_(True)
        )
    )
    if not secret_enc:
        return None
    return pyotp.TOTP(cipher.decrypt(secret_enc)).at(int(now))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: uv run python scripts/totp.py analyst@demo.pbip\n")
        return 2
    settings = get_settings()
    if settings.is_deployed:
        sys.stderr.write("Refusing to read MFA secrets from a staging/production database.\n")
        return 1
    key = settings.field_encryption_key.get_secret_value()
    if not key:
        sys.stderr.write("FIELD_ENCRYPTION_KEY is empty. Run: cp .env.example .env\n")
        return 1
    now = time.time()
    with sessionmaker(bind=get_engine())() as session:
        code = current_code(session, FieldCipher(key), argv[1], now)
    if code is None:
        sys.stderr.write("unknown user or MFA not enrolled (was the local DB seeded?)\n")
        return 1
    remaining = 30 - int(now) % 30
    sys.stdout.write(f"{code}  (valid for ~{remaining}s)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
