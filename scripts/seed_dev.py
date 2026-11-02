"""Seed a LOCAL database with demo users, funded accounts and MFA-enrolled staff.

    make seed        (or)    uv run python scripts/seed_dev.py
    docker compose exec -T -e PBIP_DEMO_PASSWORD api python - < scripts/seed_dev.py   (CI E2E job)

Refuses to run against staging/production. Safe to re-run: existing users are skipped.
Staff MFA secrets live only in the database, encrypted like any other user's; generate a login
code with:  uv run python scripts/totp.py analyst@demo.pbip
Neither the demo password nor any MFA secret is printed or written to a file.

The password for every seeded user comes from ``PBIP_DEMO_PASSWORD``. Only APP_ENV=local may fall
back to the ``DEMO_PASSWORD`` constant, so ``make seed`` keeps working on a laptop; any other
environment must pass its own. The CI E2E job always passes a freshly generated one (never printed),
so no password written in this repository guards the stack it tests.
"""

import os
import sys
import time
from collections.abc import Mapping

import pyotp
from pbip_api.core.config import get_settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.db import get_engine
from pbip_api.core.security import PasswordService
from pbip_api.modules.accounts.service import list_accounts
from pbip_api.modules.auth.models import RoleName, User
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

DEMO_PASSWORD = "Demo-Password-2026!"  # noqa: S105 - APP_ENV=local fallback only
PASSWORD_ENV = "PBIP_DEMO_PASSWORD"  # noqa: S105  # pragma: allowlist secret - a variable name

CUSTOMERS = [
    ("alice@demo.pbip", "Alice Demo", 25_000_000),  # 2,50,000.00
    ("bob@demo.pbip", "Bob Demo", 5_000_000),
]
STAFF = [
    ("analyst@demo.pbip", "Fraud Analyst", RoleName.FRAUD_ANALYST),
    ("compliance@demo.pbip", "Compliance Officer", RoleName.COMPLIANCE_OFFICER),
    ("admin@demo.pbip", "Platform Admin", RoleName.ADMIN),
    ("rm@demo.pbip", "Relationship Manager", RoleName.RELATIONSHIP_MANAGER),
]


def demo_password(app_env: str, environ: Mapping[str, str]) -> str | None:
    """``PBIP_DEMO_PASSWORD`` if set, the constant only for APP_ENV=local, else None (refuse)."""
    chosen = environ.get(PASSWORD_ENV, "")
    if chosen:
        return chosen
    return DEMO_PASSWORD if app_env == "local" else None


def main(environ: Mapping[str, str] | None = None) -> int:
    settings = get_settings()
    if settings.is_deployed:
        sys.stderr.write("Refusing to seed a staging/production database.\n")
        return 1
    if not settings.field_encryption_key.get_secret_value():
        sys.stderr.write("FIELD_ENCRYPTION_KEY is empty. Run: cp .env.example .env\n")
        return 1
    password = demo_password(settings.app_env, os.environ if environ is None else environ)
    if password is None:
        sys.stderr.write(
            "Set PBIP_DEMO_PASSWORD: only APP_ENV=local has a default demo password.\n"
        )
        return 1

    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with factory() as session:
        service = AuthService(
            session,
            settings,
            PasswordService.from_settings(settings),
            FieldCipher(settings.field_encryption_key.get_secret_value()),
        )
        meta = RequestMeta(ip="127.0.0.1", user_agent="seed-script")

        def exists(email: str) -> bool:
            return session.execute(select(User.id).where(User.email == email)).first() is not None

        for email, name, funds in CUSTOMERS:
            if exists(email):
                continue
            user = service.register_customer(email, password, name, meta)
            customer = session.execute(
                select(Customer).where(Customer.user_id == user.id)
            ).scalar_one()
            account = list_accounts(session, customer.id)[0]
            ledger.deposit(session, account.id, funds, reference="Demo opening balance")
            session.commit()
            sys.stdout.write(f"customer {email}  account {account.account_number}\n")

        for email, name, role in STAFF:
            if exists(email):
                continue
            user = service.create_user(email, password, name, [role], meta)
            enrollment = service.begin_mfa_enrollment(user.id)
            now = time.time()

            def fixed_clock(saved_now: float = now) -> float:
                return saved_now

            service.clock = fixed_clock
            service.confirm_mfa_enrollment(
                user.id, pyotp.TOTP(enrollment.secret).at(int(now)), meta
            )
            sys.stdout.write(f"staff    {email}  ({role.value}, MFA enrolled)\n")

    sys.stdout.write(
        "\nDemo password (all new users): PBIP_DEMO_PASSWORD if set, else (APP_ENV=local only)\n"
        "the DEMO_PASSWORD constant in scripts/seed_dev.py.\n"
        "Staff MFA code: uv run python scripts/totp.py <staff email>\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
