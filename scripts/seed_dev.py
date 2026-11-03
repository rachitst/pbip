"""Seed a LOCAL database with demo users, funded accounts and MFA-enrolled staff and Alice.

    make seed        (or)    uv run python scripts/seed_dev.py
    docker compose exec -T -e PBIP_DEMO_PASSWORD api python - < scripts/seed_dev.py   (CI E2E job)

Refuses to run against staging/production. Safe to re-run: existing users are skipped, and a demo
customer who already has a relationship manager keeps them (ADR-0032).
Staff MFA secrets live only in the database, encrypted like any other user's; generate a login
code with:  uv run python scripts/totp.py analyst@demo.pbip

Demo customers and what each is for:
  alice@demo.pbip  has an authenticator (``MFA_CUSTOMERS``), so the presenter can show the
                   step-up dialog on a large transfer. Her current code:
                   uv run python scripts/totp.py alice@demo.pbip
  bob@demo.pbip    no authenticator: the payee in Alice's demos and the F6 sender in the fraud lab
  carol@demo.pbip  no authenticator: the fraud demo lab's customer (F1-F5) and mule (F6). The lab
                   signs in with a password only, so it refuses a customer with an authenticator.
Re-running on an existing database adds what is missing (Carol, Alice's authenticator) and leaves
everything else alone, so it is also how an older demo database gets them.
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
from uuid import UUID

import pyotp
from pbip_api.core.config import get_settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.db import get_engine
from pbip_api.core.security import PasswordService
from pbip_api.modules.accounts.service import list_accounts
from pbip_api.modules.auth.models import RoleName, User
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.insights import service as insights_service
from pbip_api.modules.ledger import service as ledger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

DEMO_PASSWORD = "Demo-Password-2026!"  # noqa: S105 - APP_ENV=local fallback only
PASSWORD_ENV = "PBIP_DEMO_PASSWORD"  # noqa: S105  # pragma: allowlist secret - a variable name

CUSTOMERS = [
    ("alice@demo.pbip", "Alice Demo", 25_000_000),  # 2,50,000.00
    ("bob@demo.pbip", "Bob Demo", 5_000_000),
    ("carol@demo.pbip", "Carol Demo", 25_000_000),  # the fraud demo lab's customer
]
# Customers with an authenticator: step-up on a large transfer to a new payee can be answered,
# instead of every such transfer going to review. Never Bob or Carol (the lab needs them without).
MFA_CUSTOMERS = ("alice@demo.pbip",)
STAFF = [
    ("analyst@demo.pbip", "Fraud Analyst", RoleName.FRAUD_ANALYST),
    ("compliance@demo.pbip", "Compliance Officer", RoleName.COMPLIANCE_OFFICER),
    ("admin@demo.pbip", "Platform Admin", RoleName.ADMIN),
    ("rm@demo.pbip", "Relationship Manager", RoleName.RELATIONSHIP_MANAGER),
]
# ADR-0032: rm@demo.pbip looks after Alice; Bob stays unassigned, so the demo shows an RM's
# portfolio next to the administrator's view of every customer. Assigned by the demo admin.
RM_EMAIL = "rm@demo.pbip"
ASSIGNING_ADMIN_EMAIL = "admin@demo.pbip"
RM_PORTFOLIO = ("alice@demo.pbip",)


def demo_password(app_env: str, environ: Mapping[str, str]) -> str | None:
    """``PBIP_DEMO_PASSWORD`` if set, the constant only for APP_ENV=local, else None (refuse)."""
    chosen = environ.get(PASSWORD_ENV, "")
    if chosen:
        return chosen
    return DEMO_PASSWORD if app_env == "local" else None


def _user_id(session: Session, email: str) -> UUID:
    return session.execute(select(User.id).where(User.email == email)).scalar_one()


def _enrol_mfa(service: AuthService, user_id: UUID, meta: RequestMeta) -> None:
    """Enrol an authenticator through the normal service, confirmed with the code for "now".

    The clock is pinned for the confirmation only, so the code and the check use one instant
    (a step boundary in between would refuse it); the service's own clock is put back after.
    """
    enrollment = service.begin_mfa_enrollment(user_id)
    now = time.time()
    real_clock = service.clock

    def fixed_clock(saved_now: float = now) -> float:
        return saved_now

    service.clock = fixed_clock
    try:
        service.confirm_mfa_enrollment(user_id, pyotp.TOTP(enrollment.secret).at(int(now)), meta)
    finally:
        service.clock = real_clock


def _enrol_demo_customers(session: Session, service: AuthService, meta: RequestMeta) -> None:
    """Give each of ``MFA_CUSTOMERS`` an authenticator unless they already have one."""
    for email in MFA_CUSTOMERS:
        user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None or user.mfa_enabled:
            continue
        _enrol_mfa(service, user.id, meta)
        sys.stdout.write(f"customer {email}  (MFA enrolled)\n")


def _assign_demo_portfolio(session: Session) -> None:
    """Assign ``RM_PORTFOLIO`` to the demo RM through the audited service; a customer who already
    has an RM (from an earlier run or an admin's change) is left alone."""
    rm_id = _user_id(session, RM_EMAIL)
    admin_id = _user_id(session, ASSIGNING_ADMIN_EMAIL)
    for email in RM_PORTFOLIO:
        customer_id = session.execute(
            select(Customer.id).join(User, User.id == Customer.user_id).where(User.email == email)
        ).scalar_one()
        if insights_service.assigned_rm(session, customer_id) is not None:
            continue
        insights_service.assign_rm(session, customer_id, rm_id, admin_id)
        sys.stdout.write(f"assigned {email}  to {RM_EMAIL}\n")


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
            _enrol_mfa(service, user.id, meta)
            sys.stdout.write(f"staff    {email}  ({role.value}, MFA enrolled)\n")

        _enrol_demo_customers(session, service, meta)
        _assign_demo_portfolio(session)

    sys.stdout.write(
        "\nDemo password (all new users): PBIP_DEMO_PASSWORD if set, else (APP_ENV=local only)\n"
        "the DEMO_PASSWORD constant in scripts/seed_dev.py.\n"
        "MFA code (staff and alice@demo.pbip): uv run python scripts/totp.py <email>\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
