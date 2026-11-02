from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.core.redis import get_redis
from pbip_api.modules.auth.dependencies import (
    Principal,
    get_clock,
    get_request_meta,
    require_roles,
)
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.exceptions import (
    ExceptionAction,
    ExceptionKind,
    ExceptionStatus,
    apply_action,
    exceptions_view,
)
from pbip_api.modules.compliance.schemas import (
    ComplianceExceptionsOut,
    ExceptionActionRequest,
    ExceptionOut,
    dashboard_out,
    exception_out,
)
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


@router.get(
    "/exceptions",
    response_model=ComplianceExceptionsOut,
    summary="Exceptions (blocked payments, lockouts, audit-chain failures) and SLA indicators",
)
def list_exceptions(
    days: int = Query(default=30, ge=1, le=90, description="Window: exceptions of the last N days"),
    kind: ExceptionKind | None = Query(default=None, description="Only this kind"),
    status_filter: ExceptionStatus | None = Query(
        default=None, alias="status", description="Only this status"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    _principal: Principal = Depends(compliance_gate),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
    redis_client: Redis = Depends(get_redis),
) -> ComplianceExceptionsOut:
    """TASK-100 dashboard data. The audit chain is fully verified at most once per 5 minutes
    (cached, ADR-0027); ``/audit-logs/verify`` still verifies on demand."""
    now = datetime.fromtimestamp(clock(), tz=UTC)
    view = exceptions_view(
        session, now, days, kind=kind, status=status_filter, limit=limit, chain_cache=redis_client
    )
    return dashboard_out(view, now)


def _act(
    action: ExceptionAction,
    kind: ExceptionKind,
    ref: str,
    body: ExceptionActionRequest,
    principal: Principal,
    session: Session,
    meta: RequestMeta,
) -> ExceptionOut:
    record = apply_action(
        session,
        kind,
        ref,
        action,
        body.reason,
        principal.user_id,
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return exception_out(record)


_ACTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such exception"},
    409: {"description": "Already acknowledged or resolved"},
}
_REF = Path(max_length=64, pattern=r"^[A-Za-z0-9-]+$", description="Reference from the list")


@router.post(
    "/exceptions/{kind}/{ref}/acknowledge",
    response_model=ExceptionOut,
    summary="Acknowledge an open exception (audited with actor and reason)",
    responses=_ACTION_RESPONSES,
)
def acknowledge_exception(
    kind: ExceptionKind,
    body: ExceptionActionRequest,
    ref: str = _REF,
    principal: Principal = Depends(compliance_gate),
    session: Session = Depends(get_session),
    meta: RequestMeta = Depends(get_request_meta),
) -> ExceptionOut:
    return _act(ExceptionAction.ACKNOWLEDGE, kind, ref, body, principal, session, meta)


@router.post(
    "/exceptions/{kind}/{ref}/resolve",
    response_model=ExceptionOut,
    summary="Resolve an exception (audited with actor and reason)",
    responses=_ACTION_RESPONSES,
)
def resolve_exception(
    kind: ExceptionKind,
    body: ExceptionActionRequest,
    ref: str = _REF,
    principal: Principal = Depends(compliance_gate),
    session: Session = Depends(get_session),
    meta: RequestMeta = Depends(get_request_meta),
) -> ExceptionOut:
    return _act(ExceptionAction.RESOLVE, kind, ref, body, principal, session, meta)
