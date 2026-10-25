from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.core.pagination import MAX_CURSOR_LENGTH, PageDirection
from pbip_api.modules.accounts import policy, service
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.accounts.schemas import (
    AccountOut,
    EntriesPage,
    OpenAccountRequest,
)
from pbip_api.modules.auth.dependencies import get_request_meta
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer

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


@router.post(
    "",
    response_model=AccountOut,
    status_code=status.HTTP_201_CREATED,
    summary="Open a new account",
)
def open_my_account(
    body: OpenAccountRequest,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    meta: RequestMeta = Depends(get_request_meta),
) -> AccountOut:
    account = service.create_customer_account(
        session=session,
        customer=customer,
        requested_type=body.account_type,
        currency=body.currency,
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return to_account_out(account)


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
    summary="Ledger entries for one of my accounts (keyset pagination)",
)
def list_my_account_entries(
    account_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(
        default=None,
        min_length=1,
        max_length=MAX_CURSOR_LENGTH,
        description="Opaque next_cursor from a previous page (legacy integer cursors accepted)",
    ),
    direction: PageDirection = Query(
        default=PageDirection.OLDER,
        description="older: newest first, walking back; newer: oldest first, walking forward",
    ),
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> EntriesPage:
    account = policy.get_owned_account(session, customer.id, account_id)
    return service.list_entries_page(session, account, limit, cursor, direction)
