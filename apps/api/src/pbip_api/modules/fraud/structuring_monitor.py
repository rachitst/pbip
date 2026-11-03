"""The F7 deposit-side structuring monitor: an hourly beat job (#274, ADR-0051).

It scans the credits customer accounts received in the last ``LOOKBACK_WINDOWS`` structuring
windows and opens an analyst alert for every new pattern ``structuring.find_patterns`` finds. The
alert uses the existing queue: a ``fraud_events`` row (decision REVIEW, model version
``MONITOR_VERSION``, reason ``STRUCTURED_DEPOSITS``) on the pattern's LAST credit, plus a
``fraud_alerts`` row. Analysts see, open and resolve it like any other alert; resolving it only
labels the outcome, because the credits are already posted (nothing is held or reversed).

What counts as a credit (the same convention as the STRUCTURING rule, ADR-0046): POSTED DEPOSIT
credits, and POSTED TRANSFER credits from an account that no holder of the receiving account also
holds (moves between a customer's own accounts are not counted). Outgoing money is not scanned
here; the per-transfer rule covers it.

Read-only on money: no ledger call, no balance, hold or entry is touched. Each account is ONE use
case in ONE transaction: an advisory lock per account (no other code takes it, so it cannot
deadlock with the payment lock order), the covered credits re-read under that lock, then the event,
the alert and the audit row, then one commit. A re-run, an overlapping window or two workers
running the job at once therefore never open a second alert for the same credit.
"""

import time
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

import structlog
from pbip_ml.fraud.contract import FraudDecision, FraudReason, FraudScoreResult, ReasonSource
from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, aliased

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_engine, utcnow
from pbip_api.core.metrics import FRAUD_MONITOR_ALERTS_TOTAL
from pbip_api.core.money import format_minor
from pbip_api.modules.accounts.models import AccountHolder, HolderRole
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.fraud import service as fraud_service
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.fraud.structuring import (
    LOOKBACK_WINDOWS,
    Credit,
    Pattern,
    StructuringBand,
    find_patterns,
)
from pbip_api.modules.ledger.models import Transaction, TransactionStatus, TransactionType

logger = structlog.get_logger(__name__)

MONITOR_NAME = "structuring_deposits"
MONITOR_VERSION = "structuring-monitor-1"
REASON_CODE = "STRUCTURED_DEPOSITS"
# REVIEW: the pattern goes to an analyst. Below BLOCK_AT (85), so the alert's severity is MEDIUM.
MONITOR_RISK = 60


def band_from(settings: Settings) -> StructuringBand:
    return StructuringBand(
        threshold_minor=settings.fraud_structuring_threshold_minor,
        band_percent=settings.fraud_structuring_band_percent,
        window=timedelta(hours=settings.fraud_structuring_window_hours),
        min_count=settings.fraud_structuring_min_count,
    )


def recent_credits(
    session: Session, band: StructuringBand, since: datetime, until: datetime,
    account_id: UUID | None = None,
) -> list[Credit]:  # fmt: skip
    """In-band POSTED credits on customer accounts in ``[since, until)``; own-account moves out."""
    receiver, sender = aliased(AccountHolder), aliased(AccountHolder)
    own_move = exists().where(
        sender.account_id == Transaction.debit_account_id,
        sender.customer_id == receiver.customer_id,
        receiver.account_id == Transaction.credit_account_id,
    )
    stmt = (
        select(Transaction.id, Transaction.credit_account_id, Transaction.created_at,
               Transaction.amount)
        .where(
            Transaction.status == TransactionStatus.POSTED,
            Transaction.created_at >= since,
            Transaction.created_at < until,
            Transaction.amount * 100 >= band.threshold_minor * (100 - band.band_percent),
            Transaction.amount < band.threshold_minor,
            exists().where(AccountHolder.account_id == Transaction.credit_account_id),
            or_(
                Transaction.type == TransactionType.DEPOSIT,
                and_(Transaction.type == TransactionType.TRANSFER, ~own_move),
            ),
        )
    )  # fmt: skip
    if account_id is not None:
        stmt = stmt.where(Transaction.credit_account_id == account_id)
    return [
        Credit(row[0], row[1], row[2], int(row[3]))
        for row in session.execute(stmt).all()
        if row[1] is not None
    ]


def covered_credits(session: Session, account_id: UUID, since: datetime) -> frozenset[UUID]:
    """Credits of this account already in a monitor alert created since ``since``."""
    rows = session.execute(
        select(FraudEvent.features["structuring_transaction_ids"].astext).where(
            FraudEvent.model_version == MONITOR_VERSION,
            FraudEvent.features["structuring_account_id"].astext == str(account_id),
            FraudEvent.created_at >= since,
        )
    ).scalars()
    return frozenset(UUID(i) for joined in rows if joined for i in joined.split(","))


def _primary_customer(session: Session, account_id: UUID) -> UUID | None:
    return session.execute(
        select(AccountHolder.customer_id)
        .where(AccountHolder.account_id == account_id)
        .order_by(AccountHolder.holder_role != HolderRole.PRIMARY, AccountHolder.customer_id)
        .limit(1)
    ).scalar_one_or_none()


def _result(pattern: Pattern, band: StructuringBand, started: float) -> FraudScoreResult:
    first, last, count = pattern.credits[0], pattern.last, len(pattern.credits)
    hours = (last.created_at - first.created_at).total_seconds() / 3600
    message = (
        f"{count} credits just under {format_minor(band.threshold_minor, 'INR')} "
        f"(at least {100 - band.band_percent}% of it) within {hours:.1f} h; together "
        f"{format_minor(pattern.total_minor, 'INR')}"
    )
    return FraudScoreResult(
        risk_score=MONITOR_RISK,
        decision=FraudDecision.REVIEW,
        reasons=[
            FraudReason(
                code=REASON_CODE, message=message, source=ReasonSource.RULE,
                contribution=MONITOR_RISK,
            )
        ],
        model_version=MONITOR_VERSION,
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        features={
            "monitor": MONITOR_NAME,
            "structuring_account_id": str(pattern.account_id),
            "structuring_count": count,
            "structuring_total_minor": pattern.total_minor,
            "structuring_first_at": first.created_at.isoformat(),
            "structuring_last_at": last.created_at.isoformat(),
            "structuring_threshold_minor": band.threshold_minor,
            "structuring_band_percent": band.band_percent,
            "structuring_window_hours": band.window.total_seconds() / 3600,
            # Staff-only (analyst queue). Every id is kept: it is what makes a credit "covered".
            "structuring_transaction_ids": ",".join(str(c.transaction_id) for c in pattern.credits),
        },
    )  # fmt: skip


def alert_account(session: Session, account_id: UUID, band: StructuringBand, now: datetime) -> int:
    """Open an alert for each new pattern on one account; returns how many. Commits once.

    The caller's session must hold no transaction. Everything is re-read under the account's
    advisory lock, so a concurrent run of the job sees this run's alerts and skips them.
    """
    started = time.perf_counter()
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"fraud:structuring-monitor:{account_id}"},
    )
    since = now - LOOKBACK_WINDOWS * band.window
    credits = recent_credits(session, band, since, now, account_id)
    # Alerts are created after their credits, so "created since the lookback" finds every alert
    # that covers a credit still inside the lookback.
    patterns = find_patterns(credits, band, covered_credits(session, account_id, since))
    customer_id = _primary_customer(session, account_id)
    if not patterns or customer_id is None:
        session.rollback()
        return 0
    audit = AuditService(session)
    for pattern in patterns:
        result = _result(pattern, band, started)
        last_id = cast(UUID, pattern.last.transaction_id)  # recent_credits builds UUID ids
        event = fraud_service.record_event(session, last_id, customer_id, result)
        alert = fraud_service.open_alert(session, event)
        audit.record(
            None,
            "FRAUD_STRUCTURING_ALERT_OPENED",
            "fraud_alert",
            str(alert.id),
            details={
                "actor": "scheduler",
                "monitor": MONITOR_NAME,
                "account_id": str(account_id),
                "credits": len(pattern.credits),
                "total_minor": pattern.total_minor,
            },
        )
    session.commit()
    FRAUD_MONITOR_ALERTS_TOTAL.labels(MONITOR_NAME).inc(len(patterns))
    return len(patterns)


def _accounts_with_patterns(credits: Sequence[Credit], band: StructuringBand) -> list[UUID]:
    """Accounts worth locking: a pattern exists before coverage is checked."""
    by_account: dict[UUID, list[Credit]] = defaultdict(list)
    for credit in credits:
        by_account[cast(UUID, credit.account_id)].append(credit)  # built by recent_credits
    return sorted(a for a, group in by_account.items() if find_patterns(group, band))


def run_structuring_monitor(
    engine: Engine | None = None,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """The beat job. Each account in its own session and transaction, so one failure never rolls
    back or blocks another (retried next run). ``now`` is injected by tests."""
    started = time.monotonic()
    db_engine = engine or get_engine()
    band = band_from(settings or get_settings())
    moment = now or utcnow()
    with Session(db_engine) as session:
        candidates = _accounts_with_patterns(
            recent_credits(session, band, moment - LOOKBACK_WINDOWS * band.window, moment), band
        )
    counts = {"accounts": len(candidates), "alerts": 0, "errors": 0}
    for account_id in candidates:
        with Session(db_engine, expire_on_commit=False) as session:
            try:
                counts["alerts"] += alert_account(session, account_id, band, moment)
            except Exception:  # one account must never stop the batch; retried next run
                logger.exception("structuring_monitor_failed", account_id=str(account_id))
                session.rollback()
                counts["errors"] += 1
    logger.info(
        "structuring_monitor_run", duration_ms=round((time.monotonic() - started) * 1000), **counts
    )
    return counts
