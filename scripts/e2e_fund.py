"""Fund an E2E test customer's account (LOCAL/CI stacks only; TASK-077).

    docker compose exec -T api python - <email> <amount_minor> [--salary <m>] < scripts/e2e_fund.py
    uv run python scripts/e2e_fund.py <email> <amount_minor> [--salary <minor>]

PBIP has no customer-facing deposit: money arrives as an opening balance. The Playwright suite
registers a fresh customer for every journey and funds it here, through ``ledger.deposit`` (the
same sanctioned ledger path ``seed_dev.py`` uses), so each journey starts from a known balance and
never drains or alters the seeded demo customers. The script reads the code from stdin inside the
API container because the image ships no ``scripts/``.

``--salary`` also gives the customer a salary history first: the same monthly amount credited
at 10:00 IST on the 1st of each of the two previous months, back-dated through
``ledger.deposit(posted_at=...)`` exactly as ``seed_dev.py`` builds the demo salaries (ADR-0048).
Two months of one recurring source is regular income (ADR-0043), so the loan journey can check
eligibility and apply. The opening balance is posted after it, now.

Refuses staging/production, and refuses any address outside the reserved ``example.com`` domain
(RFC 2606), so it can never put money into a real or demo customer's account.
"""

import sys
from datetime import datetime

from pbip_api.core.calendar import BANK_TZ
from pbip_api.core.config import get_settings
from pbip_api.core.db import get_engine, utcnow
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
SALARY_MONTHS = 2  # the two calendar months before the running one (ADR-0043 needs two)
SALARY_HOUR = 10  # IST on the 1st, as seed_dev.py credits the demo salaries


class FundingRefusedError(Exception):
    """The request is outside what this test-only script may do."""


def salary_dates(now: datetime) -> list[datetime]:
    """10:00 IST on the 1st of each of the ``SALARY_MONTHS`` months before ``now``'s, oldest first.
    Always in the past, whatever the time of day or month the journey runs."""
    local = now.astimezone(BANK_TZ)
    dates = []
    for back in range(SALARY_MONTHS, 0, -1):
        index = local.year * 12 + local.month - 1 - back
        dates.append(datetime(index // 12, index % 12 + 1, 1, SALARY_HOUR, tzinfo=BANK_TZ))
    return dates


def fund(session: Session, email: str, amount_minor: int, salary_minor: int = 0) -> None:
    """Deposit ``amount_minor`` into the customer's first account and commit; with
    ``salary_minor``, credit a two-month salary history before it."""
    address = normalize_email(email)
    if not address.endswith(TEST_DOMAIN):
        raise FundingRefusedError(f"only {TEST_DOMAIN} test customers can be funded")
    if not 0 < amount_minor <= MAX_FUND_MINOR:
        raise FundingRefusedError(f"amount must be 1..{MAX_FUND_MINOR} minor units")
    if not 0 <= salary_minor <= MAX_FUND_MINOR:
        raise FundingRefusedError(f"salary must be 0..{MAX_FUND_MINOR} minor units")
    customer_id = session.scalar(
        select(Customer.id).join(User, User.id == Customer.user_id).where(User.email == address)
    )
    if customer_id is None:
        raise FundingRefusedError("no customer with that email")
    accounts = list_accounts(session, customer_id)
    if not accounts:
        raise FundingRefusedError("the customer has no account")
    if salary_minor:
        for at in salary_dates(utcnow()):
            reference = f"Salary {at:%B %Y}"
            ledger.deposit(session, accounts[0].id, salary_minor, reference=reference, posted_at=at)
    ledger.deposit(session, accounts[0].id, amount_minor, reference=REFERENCE)
    session.commit()


def _salary_arg(rest: list[str]) -> int | None:
    """``[]`` -> 0, ``["--salary", "<digits>"]`` -> the amount, anything else -> None (usage)."""
    if not rest:
        return 0
    if len(rest) == 2 and rest[0] == "--salary" and rest[1].isdigit():
        return int(rest[1])
    return None


def main(argv: list[str]) -> int:
    salary = _salary_arg(argv[3:])
    if len(argv) < 3 or not argv[2].isdigit() or salary is None:
        sys.stderr.write(
            "usage: python scripts/e2e_fund.py <email> <amount_minor> [--salary <minor>]\n"
        )
        return 2
    if get_settings().is_deployed:
        sys.stderr.write("Refusing to fund accounts in a staging/production database.\n")
        return 1
    try:
        with sessionmaker(bind=get_engine())() as session:
            fund(session, argv[1], int(argv[2]), salary)
    except FundingRefusedError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write("funded\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
