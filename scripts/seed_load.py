"""Seed a LOCAL database with the customers the k6 load suite logs in as (TASK-078).

    uv run python scripts/seed_load.py [--users 200] [--funds-minor 100000000]

Creates ``load0000@perf.pbip`` ... with one funded account each and one VERIFIED internal payee:
user i pays user i+1 (wrapping around), so every transfer in the load test moves money between two
seeded accounts through the real ledger. Funding uses ``ledger.deposit`` like ``seed_dev.py``.
Safe to re-run: existing users are skipped. Refuses to run against staging/production.

The password is a local-only constant shared with ``infra/k6/load.js`` (never a real credential).
"""

from __future__ import annotations

import argparse
import sys

from pbip_api.core.config import get_settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.db import get_engine
from pbip_api.core.security import PasswordService
from pbip_api.modules.accounts.service import list_accounts
from pbip_api.modules.auth.models import User
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.payees.models import Payee
from pbip_api.modules.payees.service import PayeeService
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

LOAD_PASSWORD = "Load-Test-Password-2026!"  # noqa: S105  # pragma: allowlist secret - local load data
EMAIL = "load{:04d}@perf.pbip"


def _customer_and_account(session: Session, email: str) -> tuple[Customer, str]:
    customer = session.execute(
        select(Customer).join(User, User.id == Customer.user_id).where(User.email == email)
    ).scalar_one()
    return customer, list_accounts(session, customer.id)[0].account_number


def _ensure_user(session: Session, service: AuthService, email: str, name: str, funds: int) -> bool:
    if session.execute(select(User.id).where(User.email == email)).first() is not None:
        return False
    user = service.register_customer(email, LOAD_PASSWORD, name, RequestMeta("127.0.0.1", "seed"))
    customer = session.execute(select(Customer).where(Customer.user_id == user.id)).scalar_one()
    ledger.deposit(session, list_accounts(session, customer.id)[0].id, funds, reference="Load test")
    session.commit()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--funds-minor", type=int, default=100_000_000)  # 10,00,000.00 each
    args = parser.parse_args(argv)
    settings = get_settings()
    if settings.is_deployed:
        sys.stderr.write("Refusing to seed a staging/production database.\n")
        return 1
    if args.users < 2:
        sys.stderr.write("--users must be at least 2 (each user pays the next one).\n")
        return 2

    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with factory() as session:
        service = AuthService(
            session,
            settings,
            PasswordService.from_settings(settings),
            FieldCipher(settings.field_encryption_key.get_secret_value()),
        )
        created = sum(
            _ensure_user(session, service, EMAIL.format(i), f"Load User {i:04d}", args.funds_minor)
            for i in range(args.users)
        )
        payees = PayeeService(session, settings)
        verified = 0
        for i in range(args.users):
            j = (i + 1) % args.users
            customer, _ = _customer_and_account(session, EMAIL.format(i))
            if session.execute(select(Payee.id).where(Payee.customer_id == customer.id)).first():
                continue
            _, target_account = _customer_and_account(session, EMAIL.format(j))
            payee = payees.create(
                customer.id,
                f"Load User {j:04d}",
                target_account,
                settings.internal_bank_code,
                None,
            )
            if payees.verify(payee) == "MATCH":
                verified += 1
    sys.stdout.write(
        f"load users: {created} created, {verified} payees verified ({args.users} total)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
