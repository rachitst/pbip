"""HTTP router for customer notifications inbox."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.auth.dependencies import Principal, get_current_principal
from pbip_api.modules.notifications import service
from pbip_api.modules.notifications.schemas import (
    BulkMarkReadResponse,
    NotificationOut,
    NotificationsPage,
)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("", response_model=NotificationsPage, summary="My notifications")
def list_my_notifications(
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> NotificationsPage:
    items, total, unread_count = service.list_notifications(
        session, principal.user_id, unread_only, limit=limit, offset=offset
    )
    return NotificationsPage(
        items=[
            NotificationOut(
                id=n.id,
                type=n.type,
                title=n.title,
                body=n.body,
                read=n.read_at is not None,
                created_at=n.created_at,
            )
            for n in items
        ],
        total=total,
        unread_count=unread_count,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/mark-all-read",
    response_model=BulkMarkReadResponse,
    summary="Mark all notifications as read",
)
def mark_all_notifications_read(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> BulkMarkReadResponse:
    count = service.mark_all_read(session, principal.user_id)
    return BulkMarkReadResponse(updated_count=count)


@router.post(
    "/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark as read",
)
def read_notification(
    notification_id: UUID,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> None:
    service.mark_read(session, principal.user_id, notification_id)
