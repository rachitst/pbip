"""Object-level authorization for accounts.

The ONLY way application code obtains an account on behalf of a customer. A missing account and
someone else's account are indistinguishable (404), so ids cannot be probed.
"""

from uuid import UUID

from sqlalchemy import BigInteger, literal_column, select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.accounts.models import Account, AccountHolder


def get_owned_account(session: Session, customer_id: UUID, account_id: UUID) -> Account:
    account = session.execute(
        select(Account)
        .join(AccountHolder, AccountHolder.account_id == Account.id)
        .where(Account.id == account_id, AccountHolder.customer_id == customer_id)
    ).scalar_one_or_none()
    if account is None:
        raise NotFoundError("account not found")
    return account


def list_owned_accounts(session: Session, customer_id: UUID) -> list[Account]:
    """Every account the customer holds (PRIMARY or JOINT), oldest first; nobody else's."""
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


# PostgreSQL's ``xmin`` of the row: the id of the transaction that wrote this row version. Every
# UPDATE writes a new version, so it changes whenever the ledger posts to the account, places or
# releases a hold, or its status changes; a plain read or a ``FOR UPDATE`` lock leaves it alone.
_ROW_VERSION = literal_column("accounts.xmin::text::bigint", BigInteger)


def list_owned_accounts_with_versions(
    session: Session, customer_id: UUID
) -> list[tuple[Account, int]]:
    """``list_owned_accounts`` plus each account's row version, in the same single query.

    For read caches (ADR-0053): a result derived from these accounts' ledger history is still valid
    while every version, and the set of accounts, is unchanged.
    """
    rows = session.execute(
        select(Account, _ROW_VERSION)
        .join(AccountHolder, AccountHolder.account_id == Account.id)
        .where(AccountHolder.customer_id == customer_id)
        .order_by(Account.created_at)
    ).all()
    return [(account, int(version)) for account, version in rows]
