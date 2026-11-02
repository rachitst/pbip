"""Print the current TOTP code for a seeded demo staff user (LOCAL database only).

    uv run python scripts/totp.py analyst@demo.pbip
    uv run python scripts/totp.py analyst@demo.pbip --unused

The MFA secret is read from the local database and decrypted with the app's FieldCipher (the same
FIELD_ENCRYPTION_KEY the API uses), so no plaintext copy of a secret is ever written to disk.
Refuses to run against staging/production, like the seed script.

``--unused`` is for automation (the Playwright suite, TASK-077). The API accepts a code for the
previous, current or next 30-second step, and each step only once (replay protection). A second
sign-in in the same window would be refused with the plain current code, so this prints the
earliest step the API has not seen yet and, only if both the current and the next step are used
already, waits until that step enters the accepted window. It prints only the code.
"""

import sys
import time
from collections.abc import Callable

import pyotp
from pbip_api.core.config import get_settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.db import get_engine
from pbip_api.modules.auth.models import User
from pbip_api.modules.auth.service import normalize_email
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

STEP_SECONDS = 30  # the API's TOTP step (auth/service.py)


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


def next_unused_step(last_step: int | None, now: float) -> tuple[int, float]:
    """The earliest step the API accepts that it has not seen, and the seconds until it does.

    The API takes steps current-1..current+1 that are greater than the user's last used step, so a
    step is usable from the moment the clock reaches the step before it.
    """
    current = int(now // STEP_SECONDS)
    step = current if last_step is None else max(current, last_step + 1)
    return step, max(0.0, (step - 1) * STEP_SECONDS - now)


def unused_code(
    session: Session,
    cipher: FieldCipher,
    email: str,
    now: float,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """A code the API accepts and has not seen (``sleep``s up to one step if two are used)."""
    row = session.execute(
        select(User.mfa_secret_enc, User.mfa_last_step).where(
            User.email == normalize_email(email), User.mfa_enabled.is_(True)
        )
    ).first()
    if row is None or not row.mfa_secret_enc:
        return None
    step, wait = next_unused_step(row.mfa_last_step, now)
    if wait > 0:
        sleep(wait)  # at most one step: never more than two codes ahead of the clock
    return pyotp.TOTP(cipher.decrypt(row.mfa_secret_enc)).at(step * STEP_SECONDS)


def main(argv: list[str]) -> int:
    unused = argv[2:] == ["--unused"]
    if len(argv) != 2 and not unused:
        sys.stderr.write("usage: uv run python scripts/totp.py analyst@demo.pbip [--unused]\n")
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
        cipher = FieldCipher(key)
        if unused:
            code = unused_code(session, cipher, argv[1], now)
        else:
            code = current_code(session, cipher, argv[1], now)
    if code is None:
        sys.stderr.write("unknown user or MFA not enrolled (was the local DB seeded?)\n")
        return 1
    if unused:
        sys.stdout.write(f"{code}\n")
        return 0
    remaining = 30 - int(now) % 30
    sys.stdout.write(f"{code}  (valid for ~{remaining}s)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
