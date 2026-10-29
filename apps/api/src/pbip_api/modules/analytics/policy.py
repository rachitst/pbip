"""Authorization policy for analytics endpoints.

Guarantees BOLA protection: non-owned and non-existent accounts are both 404.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from pbip_api.modules.accounts.models import Account
from pbip_api.modules.accounts.policy import get_owned_account


def verify_account_ownership(session: Session, customer_id: UUID, account_id: UUID) -> Account:
    """Verify that the authenticated customer owns the given account.

    Raises NotFoundError (404) if the account does not exist or belongs to another user.
    """
    return get_owned_account(session, customer_id, account_id)
