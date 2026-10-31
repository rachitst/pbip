from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.auth.dependencies import Principal, require_roles
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.service import AuditService

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])

compliance_gate = require_roles(RoleName.COMPLIANCE_OFFICER, RoleName.ADMIN, mfa=True)


class AuditLogOut(BaseModel):
    seq: int
    actor_user_id: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    request_id: str | None
    ip: str | None = None
    details: dict[str, Any]
    created_at: datetime
    hash: str


class ChainStatusOut(BaseModel):
    valid: bool
    checked: int
    first_invalid_seq: int | None


@router.get("/audit-logs", response_model=list[AuditLogOut], summary="Audit log, newest first")
def list_audit_logs(
    limit: int = Query(default=50, ge=1, le=200),
    before_seq: int | None = Query(default=None, ge=1, le=9_223_372_036_854_775_807),  # bigint
    _principal: Principal = Depends(compliance_gate),
    session: Session = Depends(get_session),
) -> list[AuditLogOut]:
    rows = AuditService(session).list_logs(limit, before_seq)
    return [
        AuditLogOut(
            seq=r.seq,
            actor_user_id=r.actor_user_id,
            action=r.action,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            request_id=r.request_id,
            ip=r.ip,
            details=r.details,
            created_at=r.created_at,
            hash=r.hash,
        )
        for r in rows
    ]


@router.get(
    "/audit-logs/verify",
    response_model=ChainStatusOut,
    summary="Re-verify the audit hash chain end to end",
)
def verify_audit_chain(
    _principal: Principal = Depends(compliance_gate),
    session: Session = Depends(get_session),
) -> ChainStatusOut:
    status_ = AuditService(session).verify_chain()
    return ChainStatusOut(
        valid=status_.valid, checked=status_.checked, first_invalid_seq=status_.first_invalid_seq
    )
