"""Pydantic schemas for the notifications module."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class NotificationOut(BaseModel):
    id: UUID
    type: str
    title: str
    body: str
    read: bool
    created_at: datetime
    resource_type: Literal["transaction", "security"] | None = Field(
        default=None, description="What the notification is about, when it links somewhere"
    )
    resource_id: UUID | None = Field(default=None, description="The transaction, for transfers")
    account_id: UUID | None = Field(
        default=None, description="The account the transaction was sent from, for transfers"
    )


class NotificationsPage(BaseModel):
    items: list[NotificationOut] = Field(description="Paginated list of notifications")
    total: int = Field(ge=0, description="Total number of notifications matching filter")
    unread_count: int = Field(ge=0, description="Total unread notifications for the user")
    limit: int = Field(gt=0, description="Maximum items per page")
    offset: int = Field(ge=0, description="Offset of the current page")


class BulkMarkReadResponse(BaseModel):
    updated_count: int = Field(ge=0, description="Number of notifications marked as read")
