"""Re-encrypt the field-level encrypted columns under the current primary FIELD_ENCRYPTION_KEY.

    uv run python scripts/rotate_field_key.py           # dry run: proves every row decrypts
    uv run python scripts/rotate_field_key.py --apply   # re-encrypts every row, one transaction

Procedure (docs/operations/runbook.md section 3.2): set FIELD_ENCRYPTION_KEY to "<new>,<old>",
restart the API, run this with --apply, then remove the old key. If any row cannot be decrypted
with the configured keys nothing is written and the exit code is 1, so a lost key is caught
before it becomes unrecoverable data.
"""

import argparse
import sys

from pbip_api.core.config import get_settings
from pbip_api.core.crypto import CryptoError, FieldCipher
from pbip_api.core.db import get_engine
from pbip_api.modules.auth.models import User
from pbip_api.modules.customers.models import Customer
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

# Every column that stores FieldCipher output. Add new encrypted columns here and in the runbook.
ENCRYPTED_COLUMNS: tuple[tuple[type[User] | type[Customer], str], ...] = (
    (User, "mfa_secret_enc"),
    (Customer, "phone_enc"),
)


def rotate_rows(session: Session, cipher: FieldCipher, *, apply: bool) -> dict[str, int]:
    """Re-encrypt every non-null value of each encrypted column with the cipher's primary key.

    Returns the number of rows per column. A dry run (``apply=False``) still decrypts every
    value, so it fails the same way a real run would if a key is missing, but rolls back instead
    of committing.
    """
    counts: dict[str, int] = {}
    for model, column in ENCRYPTED_COLUMNS:
        attribute = getattr(model, column)
        rows = session.execute(select(model).where(attribute.is_not(None))).scalars().all()
        for row in rows:
            rotated = cipher.rotate(getattr(row, column))
            if apply:
                setattr(row, column, rotated)
        counts[f"{model.__tablename__}.{column}"] = len(rows)
    if apply:
        session.commit()
    else:
        session.rollback()
    return counts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true", help="write the re-encrypted values (default: dry run)"
    )
    args = parser.parse_args(argv[1:])

    keys = get_settings().field_encryption_key.get_secret_value()
    if not keys:
        sys.stderr.write("FIELD_ENCRYPTION_KEY is empty; nothing to rotate.\n")
        return 1
    try:
        cipher = FieldCipher(keys)
    except CryptoError as exc:
        sys.stderr.write(f"FIELD_ENCRYPTION_KEY is invalid: {exc}\n")
        return 1
    key_count = len([key for key in keys.split(",") if key.strip()])

    with sessionmaker(bind=get_engine())() as session:
        try:
            counts = rotate_rows(session, cipher, apply=args.apply)
        except CryptoError as exc:
            session.rollback()
            sys.stderr.write(
                f"Aborted, nothing written: a row {exc}. Is the previous key still listed?\n"
            )
            return 1

    verb = "re-encrypted" if args.apply else "would re-encrypt (dry run)"
    for column, count in counts.items():
        sys.stdout.write(f"{verb} {count} row(s) in {column}\n")
    sys.stdout.write(f"keys configured: {key_count} (the first one is the primary)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
