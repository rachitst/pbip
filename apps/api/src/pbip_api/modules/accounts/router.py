from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.accounts import policy, service
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.accounts.schemas import AccountOut, EntriesPage, EntryOut
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger_service

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


def to_account_out(account: Account) -> AccountOut:
    return AccountOut(
        id=account.id,
        account_number=account.account_number,
        account_number_masked=service.mask_account_number(account.account_number),
        account_type=account.account_type.value,
        currency=account.currency,
        status=account.status.value,
        ledger_balance_minor=account.ledger_balance,
        held_minor=account.held_amount,
        available_minor=account.available_balance,
    )


@router.get("", response_model=list[AccountOut], summary="List my accounts")
def list_my_accounts(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> list[AccountOut]:
    return [to_account_out(a) for a in service.list_accounts(session, customer.id)]


@router.get("/{account_id}", response_model=AccountOut, summary="Get one of my accounts")
def get_my_account(
    account_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> AccountOut:
    return to_account_out(policy.get_owned_account(session, customer.id, account_id))


@router.get(
    "/{account_id}/transactions",
    response_model=EntriesPage,
    summary="Ledger entries for one of my accounts (newest first, keyset pagination)",
)
def list_my_account_entries(
    account_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: int | None = Query(default=None, ge=1),
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> EntriesPage:
    account = policy.get_owned_account(session, customer.id, account_id)
    rows = ledger_service.list_account_entries(session, account.id, limit + 1, cursor)
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
    return EntriesPage(items=items, next_cursor=str(page[-1][0].seq) if has_more and page else None)
