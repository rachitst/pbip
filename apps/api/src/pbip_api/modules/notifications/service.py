"""Transactional outbox + in-app notifications."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.money import format_minor
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_api.modules.notifications.policy import get_owned_notification

logger = structlog.get_logger(__name__)

TRANSFER_POSTED = "TRANSFER_POSTED"
TRANSFER_DECLINED = "TRANSFER_DECLINED"
TRANSFER_UNDER_REVIEW = "TRANSFER_UNDER_REVIEW"
BILL_PAYMENT_POSTED = "BILL_PAYMENT_POSTED"
BILL_PAYMENT_DECLINED = "BILL_PAYMENT_DECLINED"
BILL_PAYMENT_UNDER_REVIEW = "BILL_PAYMENT_UNDER_REVIEW"
# Scheduled transfer notifications (ADR-0023).
SCHEDULED_TRANSFER_FAILED = "SCHEDULED_TRANSFER_FAILED"
SCHEDULED_TRANSFER_MISSED = "SCHEDULED_TRANSFER_MISSED"
SCHEDULED_TRANSFER_PAUSED = "SCHEDULED_TRANSFER_PAUSED"
# A fraud analyst lifted a FRAUD_BLOCK pause (#304); the order waits for the customer to resume.
SCHEDULED_TRANSFER_UNLOCKED = "SCHEDULED_TRANSFER_UNLOCKED"
# Loans (ADR-0024): both link to the ledger transaction, like transfers.
LOAN_DISBURSED = "LOAN_DISBURSED"
LOAN_REPAYMENT_POSTED = "LOAN_REPAYMENT_POSTED"
# Fixed deposits (ADR-0025): link to the booking / principal payout transaction.
FIXED_DEPOSIT_BOOKED = "FIXED_DEPOSIT_BOOKED"
FIXED_DEPOSIT_CLOSED = "FIXED_DEPOSIT_CLOSED"
# Automatic maturity payout (ADR-0029): paid, or held because the account is not active.
FIXED_DEPOSIT_MATURED = "FIXED_DEPOSIT_MATURED"
FIXED_DEPOSIT_PAYOUT_HELD = "FIXED_DEPOSIT_PAYOUT_HELD"
# Security notifications for events the auth and customers modules already record (#45).
PASSWORD_CHANGED = "PASSWORD_CHANGED"  # noqa: S105  # pragma: allowlist secret - an event type name
MFA_ENABLED = "MFA_ENABLED"
DEVICE_REGISTERED = "DEVICE_REGISTERED"
STATEMENT_DOWNLOADED = "STATEMENT_DOWNLOADED"
# Balance projection (TASK-043): at most one per user, account and calendar month.
PROJECTED_LOW_BALANCE = "PROJECTED_LOW_BALANCE"

_TRANSFER_TYPES = {
    TRANSFER_POSTED,
    TRANSFER_UNDER_REVIEW,
    TRANSFER_DECLINED,
    BILL_PAYMENT_POSTED,
    BILL_PAYMENT_DECLINED,
    BILL_PAYMENT_UNDER_REVIEW,
    LOAN_DISBURSED,
    LOAN_REPAYMENT_POSTED,
    FIXED_DEPOSIT_BOOKED,
    FIXED_DEPOSIT_CLOSED,
    FIXED_DEPOSIT_MATURED,
}
_SCHEDULED_TRANSFER_TYPES = {
    SCHEDULED_TRANSFER_FAILED,
    SCHEDULED_TRANSFER_MISSED,
    SCHEDULED_TRANSFER_PAUSED,
    SCHEDULED_TRANSFER_UNLOCKED,
}
_SECURITY_TEXT = {
    PASSWORD_CHANGED: (
        "Password changed",
        "Your password was changed and other sessions were signed out. If this wasn't you, "
        "contact the bank.",
    ),
    MFA_ENABLED: (
        "Two-step verification turned on",
        "Sign-ins now need a code from your authenticator app.",
    ),
    DEVICE_REGISTERED: (
        "New device registered",
        "A new device was registered on your account. If this wasn't you, contact the bank.",
    ),
}


def enqueue(session: Session, event_type: str, payload: dict[str, Any]) -> None:
    """Add an outbox event in the CALLER'S transaction (no PII beyond ids and amounts)."""
    session.add(OutboxEvent(event_type=event_type, payload=payload))


def enqueue_security(session: Session, event_type: str, user_id: UUID) -> None:
    """A security notification for ``user_id``, committed with the change that caused it."""
    if event_type not in _SECURITY_TEXT:
        raise ValueError(f"not a security notification type: {event_type}")
    enqueue(session, event_type, {"user_id": str(user_id)})


def enqueue_statement(
    session: Session,
    user_id: UUID,
    account_id: UUID,
    account_masked: str,
    period: dict[str, str],
    file_format: str,
) -> None:
    """Tell the customer a statement left the bank (UC-03): a security notice, like a new device."""
    enqueue(
        session,
        STATEMENT_DOWNLOADED,
        {
            "user_id": str(user_id),
            "account_id": str(account_id),
            "account_masked": account_masked,
            "start": period["start"],
            "end": period["end"],
            "format": file_format,
        },
    )


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def notify_projected_low_balance(
    session: Session,
    *,
    user_id: UUID,
    account_id: UUID,
    account_masked: str,
    month_end: date,
    predicted_minor: int,
    currency: str,
    now: datetime,
) -> bool:
    """Warn the customer once per account and calendar month that the month may end low (TASK-043).

    Written directly, in the CALLER'S transaction (the caller commits): the notification is the
    only change, so there is nothing for an outbox event to wait for. A transaction-scoped
    advisory lock on (user, account) makes the check-then-insert atomic, so two concurrent
    forecast requests cannot both send it. ``now`` is the request clock and is stored as the
    notification time, so "this month" is the same month the projection was made for. Returns
    whether a notification was added.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"projected-low-balance:{user_id}:{account_id}"},
    )
    start, end = _month_bounds(now)
    already_sent = session.execute(
        select(Notification.id)
        .where(
            Notification.user_id == user_id,
            Notification.type == PROJECTED_LOW_BALANCE,
            Notification.account_id == account_id,
            Notification.created_at >= start,
            Notification.created_at < end,
        )
        .limit(1)
    ).scalar_one_or_none()
    if already_sent is not None:
        return False
    session.add(
        Notification(
            user_id=user_id,
            type=PROJECTED_LOW_BALANCE,
            title="Your balance may run low this month",
            body=(
                f"Predicted, not exact: account {account_masked} may end "
                f"{month_end.strftime('%d %b %Y')} at about "
                f"{format_minor(predicted_minor, currency)}, after your scheduled payments. "
                "Consider moving money in or rescheduling a payment."
            ),
            resource_type=None,
            account_id=account_id,
            created_at=now,
        )
    )
    session.flush()
    return True


def _statement(event: OutboxEvent, user_id: UUID) -> Notification:
    p = event.payload
    return Notification(
        user_id=user_id,
        type=STATEMENT_DOWNLOADED,
        title="Statement downloaded",
        body=(
            f"A {str(p['format']).upper()} statement for account {p['account_masked']} "
            f"({p['start']} to {p['end']}) was downloaded. If this wasn't you, contact the bank."
        ),
        resource_type="security",
        account_id=_uuid_or_none(p.get("account_id")),
    )


def _transfer_text(event: OutboxEvent) -> tuple[str, str] | None:
    amount = format_minor(int(event.payload["amount_minor"]), str(event.payload["currency"]))
    if event.event_type == TRANSFER_POSTED:
        return "Transfer completed", f"Your transfer of {amount} was completed."
    if event.event_type == TRANSFER_UNDER_REVIEW:
        return (
            "Transfer under review",
            f"Your transfer of {amount} is being reviewed for your safety.",
        )
    if event.event_type == TRANSFER_DECLINED:
        return "Transfer not completed", f"Your transfer of {amount} could not be completed."
    if event.event_type == LOAN_DISBURSED:
        emi = format_minor(int(event.payload["emi_minor"]), str(event.payload["currency"]))
        return (
            "Loan disbursed",
            f"Your loan of {amount} was credited to your account. Your first EMI of {emi} is due "
            f"on {event.payload['first_due_date']}.",
        )
    if event.event_type == LOAN_REPAYMENT_POSTED:
        return "Loan EMI paid", f"Your loan EMI of {amount} was collected from your account."
    deposit = _deposit_text(event, amount)
    if deposit is not None:
        return deposit
    name = event.payload.get("biller_name")
    to_biller = f" to {name}" if isinstance(name, str) and name else ""
    if event.event_type == BILL_PAYMENT_POSTED:
        return "Bill payment completed", f"Your bill payment of {amount}{to_biller} was completed."
    if event.event_type == BILL_PAYMENT_UNDER_REVIEW:
        return (
            "Bill payment under review",
            f"Your bill payment of {amount}{to_biller} is being reviewed for your safety.",
        )
    if event.event_type == BILL_PAYMENT_DECLINED:
        return (
            "Bill payment not completed",
            f"Your bill payment of {amount}{to_biller} could not be completed.",
        )
    return None


def _deposit_text(event: OutboxEvent, amount: str) -> tuple[str, str] | None:
    currency = str(event.payload["currency"])
    if event.event_type == FIXED_DEPOSIT_BOOKED:
        at_maturity = format_minor(int(event.payload["maturity_amount_minor"]), currency)
        matures = date.fromisoformat(str(event.payload["maturity_date"])).strftime("%d %b %Y")
        return (
            "Fixed deposit booked",
            f"Your fixed deposit of {amount} was booked. It matures on {matures} at {at_maturity}.",
        )
    if event.event_type == FIXED_DEPOSIT_CLOSED:
        interest = format_minor(int(event.payload["interest_minor"]), currency)
        return (
            "Fixed deposit closed",
            f"{amount}, including {interest} interest, was credited to your account.",
        )
    if event.event_type == FIXED_DEPOSIT_MATURED:
        interest = format_minor(int(event.payload["interest_minor"]), currency)
        return (
            "Fixed deposit matured",
            f"Your fixed deposit matured. {amount}, including {interest} interest, was credited "
            "to your account.",
        )
    return None


def _payout_held(event: OutboxEvent, user_id: UUID) -> Notification:
    """No transaction exists yet, so the bell links nowhere; the deposit stays open (ADR-0029)."""
    amount = format_minor(int(event.payload["amount_minor"]), str(event.payload["currency"]))
    return Notification(
        user_id=user_id,
        type=event.event_type,
        title="Fixed deposit payout on hold",
        body=(
            f"Your fixed deposit has matured, but its payout of {amount} is on hold because the "
            "account it pays into is not active. It will be paid automatically once the account "
            "is active again. Contact the bank for help."
        ),
        resource_type=None,
        account_id=_uuid_or_none(event.payload.get("account_id")),
    )


def _scheduled_transfer_text(event: OutboxEvent) -> tuple[str, str] | None:
    amount = format_minor(int(event.payload["amount_minor"]), str(event.payload["currency"]))
    if event.event_type == SCHEDULED_TRANSFER_FAILED:
        failed_date = event.payload.get("failed_date", "")
        return (
            "Scheduled transfer failed",
            f"Your scheduled transfer of {amount} on {failed_date} could not be completed.",
        )
    if event.event_type == SCHEDULED_TRANSFER_PAUSED:
        return (
            "Scheduled transfer paused",
            (
                f"Your scheduled transfer of {amount} was paused. "
                "Action is required to resume transfers."
            ),
        )
    if event.event_type == SCHEDULED_TRANSFER_UNLOCKED:
        return (
            "Scheduled transfer can be resumed",
            (
                f"The bank has reviewed your scheduled transfer of {amount}. "
                "It stays paused until you resume it."
            ),
        )
    if event.event_type == SCHEDULED_TRANSFER_MISSED:
        missed_count = int(event.payload.get("missed_count", 1))
        completed = bool(event.payload.get("completed", False))
        next_date = event.payload.get("next_eligible_date")
        next_suffix = (
            " The schedule has completed."
            if completed
            else (f" Next scheduled transfer: {next_date}." if next_date else "")
        )
        if missed_count <= 1:
            first_date = event.payload.get("first_missed_date", "")
            return (
                "Scheduled transfer missed",
                (
                    f"Your scheduled transfer of {amount} scheduled for "
                    f"{first_date} was not executed.{next_suffix}"
                ),
            )
        first_date = event.payload.get("first_missed_date", "")
        last_date = event.payload.get("last_missed_date", "")
        return (
            "Scheduled transfers missed",
            (
                f"Your scheduled transfer of {amount} missed {missed_count} "
                f"occurrences between {first_date} and {last_date}.{next_suffix}"
            ),
        )
    return None


def _uuid_or_none(value: object) -> UUID | None:
    return UUID(str(value)) if value else None


def _notification(event: OutboxEvent) -> Notification | None:
    user_id = UUID(str(event.payload["user_id"]))
    if event.event_type in _TRANSFER_TYPES:
        text = _transfer_text(event)
        if text is None:
            return None
        return Notification(
            user_id=user_id,
            type=event.event_type,
            title=text[0],
            body=text[1],
            resource_type="transaction",
            resource_id=_uuid_or_none(event.payload.get("transaction_id")),
            # events enqueued before #45 carry no account id: the link opens Accounts instead
            account_id=_uuid_or_none(event.payload.get("account_id")),
        )
    if event.event_type == STATEMENT_DOWNLOADED:
        return _statement(event, user_id)
    if event.event_type == FIXED_DEPOSIT_PAYOUT_HELD:
        return _payout_held(event, user_id)
    if event.event_type in _SCHEDULED_TRANSFER_TYPES:
        text = _scheduled_transfer_text(event)
        if text is None:
            return None
        return Notification(
            user_id=user_id,
            type=event.event_type,
            title=text[0],
            body=text[1],
            resource_type=None,
            resource_id=_uuid_or_none(event.payload.get("scheduled_transfer_id")),
            account_id=_uuid_or_none(event.payload.get("account_id")),
        )
    if event.event_type in _SECURITY_TEXT:
        title, body = _SECURITY_TEXT[event.event_type]
        return Notification(
            user_id=user_id,
            type=event.event_type,
            title=title,
            body=body,
            resource_type="security",
        )
    return None


class NoOutboxHandlerError(LookupError):
    """No handler is registered for the event type (for example a worker older than the API)."""


OutboxHandler = Callable[[Session, OutboxEvent], None]


def _deliver_in_app(session: Session, event: OutboxEvent) -> None:
    """Write the event's in-app notification once: a re-delivered event inserts nothing.

    ``ON CONFLICT DO NOTHING`` on ``notifications.outbox_event_id`` is the exactly-once guarantee;
    it holds even if the event's ``processed_at`` is lost or reset (ADR-0040)."""
    notification = _notification(event)
    if notification is None:
        raise NoOutboxHandlerError(event.event_type)
    values = {
        column.key: getattr(notification, column.key)
        for column in Notification.__table__.columns
        if getattr(notification, column.key) is not None
    }
    values["outbox_event_id"] = event.id
    session.execute(
        pg_insert(Notification)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Notification.outbox_event_id])
    )


_IN_APP_TYPES = (
    _TRANSFER_TYPES
    | _SCHEDULED_TRANSFER_TYPES
    | set(_SECURITY_TEXT)
    | {STATEMENT_DOWNLOADED, FIXED_DEPOSIT_PAYOUT_HELD}
)
# The dispatch handlers by event type (ADR-0040). In-app notifications are PBIP's only consumer;
# there are no external webhooks.
OUTBOX_HANDLERS: dict[str, OutboxHandler] = dict.fromkeys(_IN_APP_TYPES, _deliver_in_app)


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff for failed deliveries. The defaults equal ``Settings``' (tested)."""

    max_attempts: int = 6
    base_seconds: int = 30
    max_seconds: int = 3600

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetryPolicy":
        return cls(
            max_attempts=settings.outbox_max_attempts,
            base_seconds=settings.outbox_retry_base_seconds,
            max_seconds=settings.outbox_retry_max_seconds,
        )

    def delay(self, failures: int) -> timedelta:
        """Wait after the ``failures``-th failed attempt: base, 2 x base, 4 x base, ... capped."""
        seconds = self.base_seconds * 2 ** min(max(failures - 1, 0), 30)
        return timedelta(seconds=min(seconds, self.max_seconds))


def _record_failure(
    session: Session, event: OutboxEvent, error: Exception, now: datetime, policy: RetryPolicy
) -> None:
    """Schedule the next attempt, or dead-letter the event after the last one (audited)."""
    event.attempts += 1
    event.last_error = type(error).__name__[:100]
    if event.attempts < policy.max_attempts:
        event.next_attempt_at = now + policy.delay(event.attempts)
        logger.warning(
            "outbox_delivery_retry_scheduled",
            event_id=str(event.id),
            event_type=event.event_type,
            attempts=event.attempts,
            next_attempt_at=event.next_attempt_at.isoformat(),
            error=event.last_error,
        )
        return
    event.dead_lettered_at = now
    logger.error(
        "outbox_event_dead_lettered",
        event_id=str(event.id),
        event_type=event.event_type,
        attempts=event.attempts,
        error=event.last_error,
    )
    AuditService(session).record(
        None,
        "OUTBOX_EVENT_DEAD_LETTERED",
        "outbox_event",
        str(event.id),
        details={
            "actor": "scheduler",
            "event_type": event.event_type,
            "attempts": event.attempts,
            "error": event.last_error,
        },
    )


def relay_outbox(
    session: Session,
    batch_size: int = 100,
    *,
    now: datetime | None = None,
    policy: RetryPolicy | None = None,
    handlers: Mapping[str, OutboxHandler] | None = None,
) -> int:
    """Dispatch due outbox events and return how many were attempted; the caller commits.

    Safe with several concurrent workers (``SKIP LOCKED``). Each event runs in its own savepoint:
    a failing handler is rolled back alone, its event retried later with backoff, and the rest of
    the batch still goes out. Deliveries and ``processed_at`` commit together in the caller's one
    transaction, so a worker killed before the commit leaves every event pending and the next run
    delivers it; ``_deliver_in_app`` is idempotent per event, so nothing is delivered twice.
    """
    moment = now or utcnow()
    retry = policy or RetryPolicy()
    registry = OUTBOX_HANDLERS if handlers is None else handlers
    events = (
        session.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.processed_at.is_(None),
                OutboxEvent.dead_lettered_at.is_(None),
                OutboxEvent.next_attempt_at <= moment,
            )
            .order_by(OutboxEvent.next_attempt_at, OutboxEvent.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for event in events:
        try:
            with session.begin_nested():
                handler = registry.get(event.event_type)
                if handler is None:
                    raise NoOutboxHandlerError(event.event_type)
                handler(session, event)
        except Exception as error:  # any handler failure is retried, never lost
            _record_failure(session, event, error, moment, retry)
            continue
        event.processed_at = moment
    session.flush()
    return len(events)


def delete_processed_events(session: Session, cutoff: datetime, limit: int) -> int:
    """Delete up to ``limit`` events processed before ``cutoff`` and return how many; the caller
    commits. Pending and dead-lettered events are never deleted (ADR-0040)."""
    expired = (
        select(OutboxEvent.id)
        .where(OutboxEvent.processed_at < cutoff)
        .order_by(OutboxEvent.processed_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    result = session.execute(
        delete(OutboxEvent).where(OutboxEvent.id.in_(expired)).returning(OutboxEvent.id)
    )
    return len(result.all())


def list_notifications(
    session: Session, user_id: UUID, unread_only: bool, limit: int, offset: int = 0
) -> tuple[list[Notification], int, int]:
    """Fetch notifications for a user with pagination and total/unread counts."""
    base_stmt = select(Notification).where(Notification.user_id == user_id)
    filtered_stmt = base_stmt.where(Notification.read_at.is_(None)) if unread_only else base_stmt

    total = session.execute(select(func.count()).select_from(filtered_stmt.subquery())).scalar_one()

    unread_count = session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    ).scalar_one()

    items_stmt = (
        filtered_stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = list(session.execute(items_stmt).scalars().all())

    return items, total, unread_count


def mark_read(session: Session, user_id: UUID, notification_id: UUID) -> Notification:
    """Mark a single notification as read if owned by user_id, returning the notification."""
    notification = get_owned_notification(session, user_id, notification_id)
    if notification.read_at is None:
        notification.read_at = utcnow()
        session.commit()
    return notification


def mark_all_read(session: Session, user_id: UUID) -> int:
    """Bulk mark all unread notifications belonging to user_id as read."""
    now = utcnow()
    result = session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=now)
    )
    # rowcount is missing from SQLAlchemy's CursorResult stubs; for an UPDATE it is the row count.
    count = int(result.rowcount)  # type: ignore[attr-defined]
    session.commit()
    return count
