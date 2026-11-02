from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.accounts import policy as accounts_policy
from pbip_api.modules.auth.dependencies import get_clock, get_request_meta
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.statements import service
from pbip_api.modules.statements.service import StatementFormat

router = APIRouter(prefix="/api/v1/accounts", tags=["statements"])

_FILE_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "description": "The statement file (attachment)",
        "content": {"application/pdf": {}, "text/csv": {}},
    }
}


@router.get(
    "/{account_id}/statements",
    response_class=Response,
    responses=_FILE_RESPONSES,
    summary="Download a statement for one of my accounts (PDF or CSV)",
)
def download_statement(
    account_id: UUID,
    start_date: date = Query(description="First day of the period (IST), inclusive"),
    end_date: date = Query(
        description="Last day of the period (IST), inclusive; not in the future"
    ),
    file_format: StatementFormat = Query(default=StatementFormat.PDF, alias="format"),
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
    meta: RequestMeta = Depends(get_request_meta),
) -> Response:
    account = accounts_policy.get_owned_account(session, customer.id, account_id)
    now = datetime.fromtimestamp(clock(), tz=UTC)
    file = service.download(
        session, customer, account, start_date, end_date, file_format, now, meta
    )
    return Response(
        content=file.content,
        media_type=file.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{file.filename}"',
            "Cache-Control": "no-store",
        },
    )
