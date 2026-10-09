"""Object-level authorization for accounts.

The ONLY way application code obtains an account on behalf of a customer. A missing account and
someone else's account are indistinguishable (404), so ids cannot be probed.
"""

from uuid import UUID

from sqlalchemy import select
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
