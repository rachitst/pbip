"""Compliance exceptions and regulatory indicators (TASK-100, UC-12 Compliance & Risk, ADR-0027).

An *exception* is a system-generated event a compliance officer must look at:

* ``BLOCKED_TRANSFER``: a payment the fraud engine blocked (a ``fraud_events`` row with decision
  ``BLOCK``);
* ``ACCOUNT_LOCKOUT``: a sign-in lockout (a ``security_events`` row of type ``ACCOUNT_LOCKED``);
* ``AUDIT_CHAIN_FAILURE``: the audit hash chain no longer verifies; the reference is the first
  sequence number that fails.

Exceptions are derived from the tables that already record those facts; nothing is copied. Their
workflow state (OPEN -> ACKNOWLEDGED -> RESOLVED, or OPEN -> RESOLVED) lives only in the
tamper-evident audit log: acknowledging or resolving appends an audit row with the actor and the
reason, and the status shown is read back from those rows. So every state change is audited by
construction and cannot be edited afterwards (ADR-0027). The status is read and changed under the
audit-chain lock, so two officers cannot both acknowledge the same exception.

Output is identifiers, amounts, codes and times only: no names, emails, account numbers or IPs.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, String, case, cast, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Subquery

from pbip_api.core.errors import ConflictError, NotFoundError
from pbip_api.modules.auth.models import SecurityEvent, SecurityEventType
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.fraud.models import AlertStatus, FraudAlert, FraudEvent
from pbip_api.modules.ledger.models import Transaction

RESOURCE_TYPE = "compliance_exception"
ACKNOWLEDGED_ACTION = "COMPLIANCE_EXCEPTION_ACKNOWLEDGED"
RESOLVED_ACTION = "COMPLIANCE_EXCEPTION_RESOLVED"
# Service levels the simulated bank sets itself (ADR-0027); no regulator prescribes these numbers.
REVIEW_SLA = timedelta(hours=24)  # an open fraud review (held payment) should be decided within it
ACKNOWLEDGE_SLA = timedelta(hours=24)  # an exception should be acknowledged within it
BLOCK_DECISION = "BLOCK"


class ExceptionKind(StrEnum):
    BLOCKED_TRANSFER = "BLOCKED_TRANSFER"
    ACCOUNT_LOCKOUT = "ACCOUNT_LOCKOUT"
    AUDIT_CHAIN_FAILURE = "AUDIT_CHAIN_FAILURE"


class ExceptionStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class ExceptionAction(StrEnum):
    ACKNOWLEDGE = "ACKNOWLEDGE"
    RESOLVE = "RESOLVE"


@dataclass(frozen=True, slots=True)
class Step:
    """Who acknowledged or resolved an exception, when, and why (from the audit row)."""

    by_user_id: UUID | None
    at: datetime
    reason: str


@dataclass(slots=True)
class ExceptionRecord:
    kind: ExceptionKind
    ref: str
    occurred_at: datetime
    status: ExceptionStatus
    acknowledged: Step | None
    resolved: Step | None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.kind.value}:{self.ref}"


@dataclass(frozen=True, slots=True)
class ExceptionsView:
    """The dashboard: indicators over the window, then the matching exceptions, newest first."""

    window_days: int
    counts: dict[ExceptionKind, dict[ExceptionStatus, int]]
    unacknowledged_past_sla: int
    reviews_pending: int
    reviews_past_sla: int
    oldest_review_at: datetime | None
    chain_valid: bool
    chain_checked: int
    chain_first_invalid_seq: int | None
    items: list[ExceptionRecord]


@dataclass(frozen=True, slots=True)
class _Chain:
    valid: bool
    checked: int
    first_invalid_seq: int | None
    record: ExceptionRecord | None


def _resource_id(kind: ExceptionKind, ref: str) -> str:
    return f"{kind.value}:{ref}"


def _sources(since: datetime | None, ref: str | None = None) -> Subquery:
    """Blocked payments and lockouts as (kind, ref, occurred_at), newest facts from their owners."""
    blocked = select(
        literal(ExceptionKind.BLOCKED_TRANSFER.value, String).label("kind"),
        cast(FraudEvent.id, String).label("ref"),
        FraudEvent.created_at.label("occurred_at"),
    ).where(FraudEvent.decision == BLOCK_DECISION)
    lockouts = select(
        literal(ExceptionKind.ACCOUNT_LOCKOUT.value, String),
        cast(SecurityEvent.id, String),
        SecurityEvent.created_at,
    ).where(SecurityEvent.event_type == SecurityEventType.ACCOUNT_LOCKED)
    if since is not None:
        blocked = blocked.where(FraudEvent.created_at >= since)
        lockouts = lockouts.where(SecurityEvent.created_at >= since)
    if ref is not None:
        blocked = blocked.where(cast(FraudEvent.id, String) == ref)
        lockouts = lockouts.where(cast(SecurityEvent.id, String) == ref)
    return union_all(blocked, lockouts).subquery("src")


def _steps(action: str) -> Subquery:
    """The first audit row of ``action`` per exception (the service writes at most one)."""
    return (
        select(
            AuditLog.resource_id.label("rid"),
            AuditLog.actor_user_id.label("by"),
            AuditLog.created_at.label("at"),
            AuditLog.details["reason"].astext.label("reason"),
        )
        .where(AuditLog.resource_type == RESOURCE_TYPE, AuditLog.action == action)
        .order_by(AuditLog.resource_id, AuditLog.seq)
        .ext(distinct_on(AuditLog.resource_id))
        .subquery()
    )


def _status(ack: Subquery, res: Subquery) -> ColumnElement[str]:
    return case(
        (res.c.rid.is_not(None), ExceptionStatus.RESOLVED.value),
        (ack.c.rid.is_not(None), ExceptionStatus.ACKNOWLEDGED.value),
        else_=ExceptionStatus.OPEN.value,
    )


def _with_state(src: Subquery) -> Select[Any]:
    ack, res = _steps(ACKNOWLEDGED_ACTION), _steps(RESOLVED_ACTION)
    rid = src.c.kind + ":" + src.c.ref
    return (
        select(
            src.c.kind,
            src.c.ref,
            src.c.occurred_at,
            _status(ack, res).label("status"),
            ack.c.by,
            ack.c.at,
            ack.c.reason,
            res.c.by.label("res_by"),
            res.c.at.label("res_at"),
            res.c.reason.label("res_reason"),
        )
        .outerjoin(ack, ack.c.rid == rid)
        .outerjoin(res, res.c.rid == rid)
    )


def _step(by: UUID | None, at: datetime | None, reason: str | None) -> Step | None:
    return None if at is None else Step(by, at, reason or "")


def _record(row: Any) -> ExceptionRecord:
    return ExceptionRecord(
        kind=ExceptionKind(row.kind),
        ref=row.ref,
        occurred_at=row.occurred_at,
        status=ExceptionStatus(row.status),
        acknowledged=_step(row.by, row.at, row.reason),
        resolved=_step(row.res_by, row.res_at, row.res_reason),
    )


def _state_of(session: Session, resource_id: str) -> tuple[Step | None, Step | None]:
    rows = session.execute(
        select(AuditLog.action, AuditLog.actor_user_id, AuditLog.created_at, AuditLog.details)
        .where(AuditLog.resource_type == RESOURCE_TYPE, AuditLog.resource_id == resource_id)
        .order_by(AuditLog.seq)
    ).all()
    found: dict[str, Step] = {}
    for action, by, at, details in rows:
        found.setdefault(action, Step(by, at, str(details.get("reason", ""))))
    return found.get(ACKNOWLEDGED_ACTION), found.get(RESOLVED_ACTION)


def _status_from(ack: Step | None, res: Step | None) -> ExceptionStatus:
    if res is not None:
        return ExceptionStatus.RESOLVED
    return ExceptionStatus.ACKNOWLEDGED if ack is not None else ExceptionStatus.OPEN


def _chain(session: Session) -> _Chain:
    """Re-verify the audit chain; a failure is one exception keyed by the first bad sequence."""
    status = AuditService(session).verify_chain()
    if status.valid or status.first_invalid_seq is None:
        return _Chain(status.valid, status.checked, status.first_invalid_seq, None)
    seq = status.first_invalid_seq
    # verify_chain only reports the sequence of a row it read, so the row exists.
    occurred = session.execute(select(AuditLog.created_at).where(AuditLog.seq == seq)).scalar_one()
    ref = str(seq)
    ack, res = _state_of(session, _resource_id(ExceptionKind.AUDIT_CHAIN_FAILURE, ref))
    record = ExceptionRecord(
        kind=ExceptionKind.AUDIT_CHAIN_FAILURE,
        ref=ref,
        occurred_at=occurred,
        status=_status_from(ack, res),
        acknowledged=ack,
        resolved=res,
        detail={"first_invalid_seq": seq, "checked": status.checked},
    )
    return _Chain(False, status.checked, seq, record)


def _counts(
    session: Session, src: Subquery, chain: ExceptionRecord | None, sla_cutoff: datetime
) -> tuple[dict[ExceptionKind, dict[ExceptionStatus, int]], int]:
    counts = {k: dict.fromkeys(ExceptionStatus, 0) for k in ExceptionKind}
    state = _with_state(src).subquery()
    for kind, status, n in session.execute(
        select(state.c.kind, state.c.status, func.count()).group_by(state.c.kind, state.c.status)
    ):
        counts[ExceptionKind(kind)][ExceptionStatus(status)] = int(n)
    late = int(
        session.scalar(
            select(func.count()).where(
                state.c.status == ExceptionStatus.OPEN.value, state.c.occurred_at <= sla_cutoff
            )
        )
        or 0
    )
    if chain is not None:
        counts[chain.kind][chain.status] += 1
        if chain.status is ExceptionStatus.OPEN and chain.occurred_at <= sla_cutoff:
            late += 1
    return counts, late


def _blocked_details(session: Session, refs: list[str]) -> dict[str, dict[str, Any]]:
    if not refs:
        return {}
    rows = session.execute(
        select(FraudEvent, Transaction.amount, Transaction.currency, Transaction.type)
        .join(Transaction, Transaction.id == FraudEvent.transaction_id)
        .where(FraudEvent.id.in_([UUID(r) for r in refs]))
    ).all()
    return {
        str(event.id): {
            "risk_score": event.risk_score,
            "amount_minor": int(amount),
            "currency": currency,
            "transaction_type": str(tx_type),
            "reason_codes": _codes(event.reasons),
        }
        for event, amount, currency, tx_type in rows
    }


def _codes(reasons: list[dict[str, Any]]) -> list[str]:
    """Rule and model codes only: messages may quote amounts or payee details, codes never do."""
    return [
        str(r["code"]) for r in reasons if isinstance(r, dict) and isinstance(r.get("code"), str)
    ]


def _reviews(session: Session, sla_cutoff: datetime) -> tuple[int, int, datetime | None]:
    """Open fraud reviews (alerts on held payments): how many, how many past SLA, the oldest."""
    is_open = FraudAlert.status == AlertStatus.OPEN
    total, late, oldest = session.execute(
        select(
            func.count(),
            func.count().filter(FraudAlert.created_at <= sla_cutoff),
            func.min(FraudAlert.created_at),
        ).where(is_open)
    ).one()
    return int(total), int(late), oldest


def exceptions_view(
    session: Session,
    now: datetime,
    window_days: int,
    *,
    kind: ExceptionKind | None = None,
    status: ExceptionStatus | None = None,
    limit: int = 50,
) -> ExceptionsView:
    """Indicators over the last ``window_days`` and the matching exceptions, newest first.

    The audit-chain failure, when there is one, is a standing condition: it is listed and counted
    whatever the window, because it stays true until the chain is investigated.
    """
    src = _sources(now - timedelta(days=window_days))
    chain = _chain(session)
    sla_cutoff = now - ACKNOWLEDGE_SLA
    counts, late = _counts(session, src, chain.record, sla_cutoff)
    query = _with_state(src).subquery()
    stmt = select(query).order_by(query.c.occurred_at.desc(), query.c.ref).limit(limit)
    if kind is not None:
        stmt = stmt.where(query.c.kind == kind.value)
    if status is not None:
        stmt = stmt.where(query.c.status == status.value)
    items = [_record(row) for row in session.execute(stmt)]
    record = chain.record
    if record is not None and kind in (None, record.kind) and status in (None, record.status):
        items.append(record)
    items.sort(key=lambda r: (r.occurred_at, r.ref), reverse=True)
    items = items[:limit]
    details = _blocked_details(
        session, [r.ref for r in items if r.kind is ExceptionKind.BLOCKED_TRANSFER]
    )
    for item in items:
        item.detail = details.get(item.ref, item.detail)
    pending, reviews_late, oldest = _reviews(session, now - REVIEW_SLA)
    return ExceptionsView(
        window_days=window_days,
        counts=counts,
        unacknowledged_past_sla=late,
        reviews_pending=pending,
        reviews_past_sla=reviews_late,
        oldest_review_at=oldest,
        chain_valid=chain.valid,
        chain_checked=chain.checked,
        chain_first_invalid_seq=chain.first_invalid_seq,
        items=items,
    )


def _canonical_ref(kind: ExceptionKind, ref: str) -> str:
    """The reference in the form the sources use; anything malformed cannot exist (404)."""
    try:
        if kind is ExceptionKind.AUDIT_CHAIN_FAILURE:
            seq = int(ref)
            if seq < 1:
                raise ValueError(ref)
            return str(seq)
        return str(UUID(ref))
    except ValueError as exc:
        raise NotFoundError("Exception not found") from exc


def _current(session: Session, kind: ExceptionKind, ref: str) -> ExceptionRecord:
    """The exception as it stands now, or 404 when no such exception exists."""
    if kind is ExceptionKind.AUDIT_CHAIN_FAILURE:
        chain = _chain(session).record
        if chain is None or chain.ref != ref:
            raise NotFoundError("Exception not found")
        return chain
    current = _with_state(_sources(None, ref)).subquery()
    row = session.execute(select(current).where(current.c.kind == kind.value)).first()
    if row is None:
        raise NotFoundError("Exception not found")
    record = _record(row)
    if record.kind is ExceptionKind.BLOCKED_TRANSFER:
        record.detail = _blocked_details(session, [record.ref]).get(record.ref, {})
    return record


def apply_action(
    session: Session,
    kind: ExceptionKind,
    ref: str,
    action: ExceptionAction,
    reason: str,
    actor_user_id: UUID,
    *,
    ip: str | None = None,
    request_id: str | None = None,
) -> ExceptionRecord:
    """Acknowledge or resolve one exception; the audit row IS the state change (one commit).

    Acknowledge needs an OPEN exception; resolve needs one that is not already resolved. Anything
    else is a 409, so a second officer acting on stale screen state learns it was already done.
    """
    canonical = _canonical_ref(kind, ref)
    audit = AuditService(session)
    audit.lock_chain()  # held to commit: the status read below cannot change under us
    record = _current(session, kind, canonical)
    if action is ExceptionAction.ACKNOWLEDGE and record.status is not ExceptionStatus.OPEN:
        raise ConflictError(f"The exception is already {record.status.value.lower()}")
    if action is ExceptionAction.RESOLVE and record.status is ExceptionStatus.RESOLVED:
        raise ConflictError("The exception is already resolved")
    row = audit.record(
        actor_user_id,
        ACKNOWLEDGED_ACTION if action is ExceptionAction.ACKNOWLEDGE else RESOLVED_ACTION,
        RESOURCE_TYPE,
        record.id,
        ip=ip,
        request_id=request_id,
        details={
            "kind": kind.value,
            "ref": canonical,
            "previous_status": record.status.value,
            "reason": reason,
        },
    )
    step = Step(actor_user_id, row.created_at, reason)
    if action is ExceptionAction.ACKNOWLEDGE:
        record.acknowledged, record.status = step, ExceptionStatus.ACKNOWLEDGED
    else:
        record.resolved, record.status = step, ExceptionStatus.RESOLVED
    session.commit()
    return record
