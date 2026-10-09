"""Account opening and read use cases."""

import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import ConflictError
from pbip_api.modules.accounts.models import Account, AccountHolder, AccountType, HolderRole

_ACCOUNT_NUMBER_PREFIX = "40"
_MAX_ATTEMPTS = 8


def luhn_check_digit(payload: str) -> str:
    total = 0
    for index, char in enumerate(reversed(payload)):
        digit = int(char)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - total % 10) % 10)


def generate_account_number() -> str:
    body = _ACCOUNT_NUMBER_PREFIX + "".join(str(secrets.randbelow(10)) for _ in range(9))
    return body + luhn_check_digit(body)


def is_valid_account_number(number: str) -> bool:
    return len(number) == 12 and number.isdigit() and luhn_check_digit(number[:-1]) == number[-1]


def mask_account_number(number: str) -> str:
    return "\u2022" * max(len(number) - 4, 0) + number[-4:]


def open_account(
    session: Session, customer_id: UUID, account_type: AccountType, currency: str = "INR"
) -> Account:
    for _ in range(_MAX_ATTEMPTS):
        number = generate_account_number()
        if (
            session.execute(select(Account.id).where(Account.account_number == number)).first()
            is None
        ):
            account = Account(account_number=number, account_type=account_type, currency=currency)
            session.add(account)
            session.flush()
            session.add(
                AccountHolder(
                    account_id=account.id, customer_id=customer_id, holder_role=HolderRole.PRIMARY
                )
            )
            session.flush()
            return account
    raise ConflictError("could not allocate an account number, please retry")


def list_accounts(session: Session, customer_id: UUID) -> list[Account]:
    return list(
        session.execute(
            select(Account)
            .join(AccountHolder, AccountHolder.account_id == Account.id)
            .where(AccountHolder.customer_id == customer_id)
            .order_by(Account.created_at)
        )
        .scalars()
        .all()
    )
