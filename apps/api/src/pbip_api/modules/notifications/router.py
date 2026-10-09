from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.auth.dependencies import Principal, get_current_principal
from pbip_api.modules.notifications import service

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: UUID
    type: str
    title: str
    body: str
    read: bool
    created_at: datetime


@router.get("", response_model=list[NotificationOut], summary="My notifications")
def list_my_notifications(
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> list[NotificationOut]:
    rows = service.list_notifications(session, principal.user_id, unread_only, limit)
    return [
        NotificationOut(
            id=n.id,
            type=n.type,
            title=n.title,
            body=n.body,
            read=n.read_at is not None,
            created_at=n.created_at,
        )
        for n in rows
    ]


@router.post(
    "/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT, summary="Mark as read"
)
def read_notification(
    notification_id: UUID,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> None:
    service.mark_read(session, principal.user_id, notification_id)
