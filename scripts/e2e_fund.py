"""Fund an E2E test customer's account (LOCAL/CI stacks only; TASK-077).

    docker compose exec -T api python - <email> <amount_minor> < scripts/e2e_fund.py
    uv run python scripts/e2e_fund.py <email> <amount_minor>

PBIP has no customer-facing deposit: money arrives as an opening balance. The Playwright suite
registers a fresh customer for every journey and funds it here, through ``ledger.deposit`` (the
same sanctioned ledger path ``seed_dev.py`` uses), so each journey starts from a known balance and
never drains or alters the seeded demo customers. The script reads the code from stdin inside the
API container because the image ships no ``scripts/``.

Refuses staging/production, and refuses any address outside the reserved ``example.com`` domain
(RFC 2606), so it can never put money into a real or demo customer's account.
"""

import sys

from pbip_api.core.config import get_settings
from pbip_api.core.db import get_engine
from pbip_api.modules.accounts.service import list_accounts
from pbip_api.modules.auth.models import User
from pbip_api.modules.auth.service import normalize_email
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

TEST_DOMAIN = "@example.com"
MAX_FUND_MINOR = 1_000_000_00  # 10,00,000.00: twice the daily transfer limit
REFERENCE = "E2E opening balance"


class FundingRefusedError(Exception):
    """The request is outside what this test-only script may do."""


def fund(session: Session, email: str, amount_minor: int) -> None:
    """Deposit ``amount_minor`` into the customer's first account and commit."""
    address = normalize_email(email)
    if not address.endswith(TEST_DOMAIN):
        raise FundingRefusedError(f"only {TEST_DOMAIN} test customers can be funded")
    if not 0 < amount_minor <= MAX_FUND_MINOR:
        raise FundingRefusedError(f"amount must be 1..{MAX_FUND_MINOR} minor units")
    customer_id = session.scalar(
        select(Customer.id).join(User, User.id == Customer.user_id).where(User.email == address)
    )
    if customer_id is None:
        raise FundingRefusedError("no customer with that email")
    accounts = list_accounts(session, customer_id)
    if not accounts:
        raise FundingRefusedError("the customer has no account")
    ledger.deposit(session, accounts[0].id, amount_minor, reference=REFERENCE)
    session.commit()


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not argv[2].isdigit():
        sys.stderr.write("usage: python scripts/e2e_fund.py <email> <amount_minor>\n")
        return 2
    if get_settings().is_deployed:
        sys.stderr.write("Refusing to fund accounts in a staging/production database.\n")
        return 1
    try:
        with sessionmaker(bind=get_engine())() as session:
            fund(session, argv[1], int(argv[2]))
    except FundingRefusedError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write("funded\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
