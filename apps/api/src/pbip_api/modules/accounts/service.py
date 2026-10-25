"""Account opening and read use cases."""

import secrets
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from pbip_api.core.errors import ConflictError, UnprocessableError
from pbip_api.core.pagination import PageDirection, decode_cursor, encode_cursor
from pbip_api.modules.accounts.models import (
    Account,
    AccountHolder,
    AccountStatus,
    AccountType,
    HolderRole,
)
from pbip_api.modules.accounts.schemas import EntriesPage, EntryOut, OpenAccountType
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger_service

_ACCOUNT_NUMBER_PREFIX = "40"
_MAX_ATTEMPTS = 8
MAX_ACTIVE_ACCOUNTS_PER_CUSTOMER = 5


class AccountLimitExceededError(UnprocessableError):
    code = "account_limit_exceeded"


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
    session: Session,
    customer_id: UUID,
    account_type: AccountType,
    currency: str = "INR",
    *,
    actor_user_id: UUID | None = None,
    ip: str | None = None,
    request_id: str | None = None,
    commit: bool = False,
) -> Account:
    if account_type == AccountType.SYSTEM:
        raise UnprocessableError("cannot open system accounts through this use case")

    # Serialize per-customer account creation to prevent race conditions bypassing limits
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:cid))"),
        {"cid": str(customer_id)},
    )

    active_count = session.execute(
        select(func.count(Account.id))
        .join(AccountHolder, AccountHolder.account_id == Account.id)
        .where(
            AccountHolder.customer_id == customer_id,
            Account.status == AccountStatus.ACTIVE,
        )
    ).scalar_one()

    if active_count >= MAX_ACTIVE_ACCOUNTS_PER_CUSTOMER:
        raise AccountLimitExceededError(
            f"customer cannot have more than {MAX_ACTIVE_ACCOUNTS_PER_CUSTOMER} active accounts"
        )

    for _ in range(_MAX_ATTEMPTS):
        number = generate_account_number()
        if (
            session.execute(select(Account.id).where(Account.account_number == number)).first()
            is None
        ):
            account = Account(
                account_number=number,
                account_type=account_type,
                currency=currency,
                status=AccountStatus.ACTIVE,
                ledger_balance=0,
                held_amount=0,
                overdraft_limit=0,
            )
            session.add(account)
            session.flush()
            session.add(
                AccountHolder(
                    account_id=account.id,
                    customer_id=customer_id,
                    holder_role=HolderRole.PRIMARY,
                )
            )
            session.flush()

            effective_actor_user_id = actor_user_id
            if effective_actor_user_id is None:
                effective_actor_user_id = session.execute(
                    select(Customer.user_id).where(Customer.id == customer_id)
                ).scalar_one_or_none()

            AuditService(session).record(
                actor_user_id=effective_actor_user_id,
                action="ACCOUNT_OPENED",
                resource_type="account",
                resource_id=str(account.id),
                ip=ip,
                request_id=request_id,
                details={
                    "account_number": mask_account_number(account.account_number),
                    "account_type": account.account_type.value,
                    "currency": account.currency,
                },
            )

            if commit:
                session.commit()

            return account

    raise ConflictError("could not allocate an account number, please retry")


def create_customer_account(
    session: Session,
    customer: Customer,
    requested_type: OpenAccountType,
    currency: str = "INR",
    ip: str | None = None,
    request_id: str | None = None,
) -> Account:
    if requested_type in (OpenAccountType.CHECKING, OpenAccountType.CURRENT):
        account_type = AccountType.CURRENT
    elif requested_type == OpenAccountType.SAVINGS:
        account_type = AccountType.SAVINGS
    else:
        raise UnprocessableError("unsupported account type")

    return open_account(
        session=session,
        customer_id=customer.id,
        account_type=account_type,
        currency=currency,
        actor_user_id=customer.user_id,
        ip=ip,
        request_id=request_id,
        commit=True,
    )


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


def list_entries_page(
    session: Session,
    account: Account,
    limit: int,
    cursor: str | None,
    direction: PageDirection,
) -> EntriesPage:
    """One keyset page of an (already authorized) account's ledger entries."""
    after_seq = decode_cursor(cursor).seq if cursor is not None else None
    rows = ledger_service.list_account_entries(
        session, account.id, limit + 1, after_seq, newer=direction == PageDirection.NEWER
    )
    page, has_more = rows[:limit], len(rows) > limit
    items = [
        EntryOut(
            entry_seq=entry.seq,
            transaction_id=txn.id,
            direction=entry.direction.value,
            amount_minor=entry.amount,
            balance_after_minor=entry.balance_after,
            transaction_type=txn.type.value,
            transaction_status=txn.status.value,
            reference=txn.reference,
            created_at=entry.created_at,
        )
        for entry, txn in page
    ]
    last = page[-1][0] if page else None
    next_cursor = encode_cursor(last.seq, last.created_at) if has_more and last else None
    return EntriesPage(items=items, next_cursor=next_cursor, has_more=has_more)
