"""Seed a LOCAL database with demo users, funded accounts and MFA-enrolled staff.

    make seed        (or)    uv run python scripts/seed_dev.py

Refuses to run against staging/production. Safe to re-run: existing users are skipped.
Staff MFA secrets live only in the database, encrypted like any other user's; generate a login
code with:  uv run python scripts/totp.py analyst@demo.pbip
Neither the demo password nor any MFA secret is printed or written to a file.
"""

import sys
import time

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

DEMO_PASSWORD = "Demo-Password-2026!"  # noqa: S105 - local demo data only

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


def main() -> int:
    settings = get_settings()
    if settings.is_deployed:
        sys.stderr.write("Refusing to seed a staging/production database.\n")
        return 1
    if not settings.field_encryption_key.get_secret_value():
        sys.stderr.write("FIELD_ENCRYPTION_KEY is empty. Run: cp .env.example .env\n")
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
            user = service.register_customer(email, DEMO_PASSWORD, name, meta)
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
            user = service.create_user(email, DEMO_PASSWORD, name, [role], meta)
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
        "\nDemo password (all users): the DEMO_PASSWORD constant in scripts/seed_dev.py.\n"
        "Staff MFA code: uv run python scripts/totp.py <staff email>\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
