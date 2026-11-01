"""Transfer use cases: validation, idempotency, limits, fraud decision, ledger posting.

Transaction boundary: ONE database transaction per request. It contains the idempotency row,
the transaction header, the fraud event/alert, the ledger entries, balance updates, the audit
row and the outbox event. Any exception before ``commit`` rolls ALL of it back, so a client can
safely retry with the same Idempotency-Key.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pbip_ml.fraud.contract import FraudDecision, FraudScorer, TransactionContext
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import ConflictError, NotFoundError, UnprocessableError
from pbip_api.core.geoip import CountryResolver, NoCountryResolver
from pbip_api.core.metrics import (
    FRAUD_DECISIONS_TOTAL,
    FRAUD_INFERENCE_SECONDS,
    IDEMPOTENT_REPLAYS_TOTAL,
    TRANSFERS_TOTAL,
)
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.accounts.models import Account, AccountStatus, AccountType
from pbip_api.modules.accounts.policy import get_owned_account
from pbip_api.modules.auth.models import User
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud import service as fraud_service
from pbip_api.modules.fraud.models import AlertStatus, FraudAlert
from pbip_api.modules.fraud.signals import ip_network
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    IdempotencyKey,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.payees.models import Payee, PayeeStatus
from pbip_api.modules.payees.policy import get_owned_payee
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    SchedulePauseReason,
)
from pbip_api.modules.payments.policy import get_owned_scheduled_transfer
from pbip_api.modules.payments.recurrence import UpcomingOccurrence, compute_next_occurrence
from pbip_api.modules.payments.schemas import ScheduledTransferCreate


class LimitExceededError(UnprocessableError):
    code = "limit_exceeded"


class IdempotencyKeyReuseError(UnprocessableError):
    code = "idempotency_key_reuse"


@dataclass(frozen=True, slots=True)
class TransferCommand:
    from_account_id: UUID
    amount_minor: int
    currency: str
    payee_id: UUID | None
    to_account_id: UUID | None
    reference: str | None


@dataclass(frozen=True, slots=True)
class TransferResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


class TransferChannel(StrEnum):
    """Where a transfer originated. Stored on ``transactions.channel`` (String(10))."""

    WEB = "WEB"
    SCHEDULED = "SCHEDULED"


@dataclass(frozen=True, slots=True)
class TransferExecution:
    """Outcome of ``_execute_transfer``: nothing here has been committed yet.

    ``transaction`` is the new transaction, or the ORIGINAL one when ``replayed`` is true (an
    idempotent replay writes nothing). The caller owns the commit, and must call
    ``PaymentsService.record_committed`` afterwards for a non-replayed execution.
    """

    outcome: TransferResult
    transaction: Transaction
    replayed: bool


_PENDING_OR_DONE = (
    TransactionStatus.POSTED,
    TransactionStatus.PENDING_REVIEW,
    TransactionStatus.PENDING_STEP_UP,
)


class PaymentsService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        scorer: FraudScorer,
        auth: AuthService,
        countries: CountryResolver | None = None,
        counters: VelocityCounters | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.scorer = scorer
        self.auth = auth
        self.countries = countries or NoCountryResolver()
        self.counters = counters
        self.audit = AuditService(session)

    # ----------------------------------------------------------------- creation
    def create_transfer(
        self,
        *,
        user_id: UUID,
        customer: Customer,
        command: TransferCommand,
        idempotency_key: str,
        meta: RequestMeta,
        device_id: UUID | None = None,
    ) -> TransferResult:
        """Interactive transfer: one use case, one commit."""
        execution = self._execute_transfer(
            user_id=user_id,
            customer=customer,
            command=command,
            idempotency_key=idempotency_key,
            meta=meta,
            device_id=device_id,
            channel=TransferChannel.WEB,
            interactive=True,
        )
        if execution.replayed:
            return execution.outcome  # a replay wrote nothing, so there is nothing to commit
        self.session.commit()
        self.record_committed(execution)
        return execution.outcome

    def record_committed(self, execution: TransferExecution) -> None:
        """Metrics and Redis counters for an execution whose transaction has been committed.

        Kept out of ``_execute_transfer`` so a rolled-back attempt is never counted.
        """
        if execution.replayed:
            return
        transaction = execution.transaction
        TRANSFERS_TOTAL.labels(transaction.status.value).inc()
        if self.counters is not None and transaction.debit_account_id is not None:
            self.counters.record_transfer(
                transaction.debit_account_id, transaction.id, transaction.amount, utcnow()
            )

    def _execute_transfer(
        self,
        *,
        user_id: UUID,
        customer: Customer,
        command: TransferCommand,
        idempotency_key: str,
        meta: RequestMeta | None,
        device_id: UUID | None,
        channel: TransferChannel,
        interactive: bool,
    ) -> TransferExecution:
        """The transfer use case WITHOUT committing or rolling back.

        The caller owns the transaction boundary, so a scheduled run can update its own state in
        the same commit. ``meta`` is None for background work: nothing is invented, the IP-derived
        signals are simply unknown and the audit row carries no IP/request id. ``interactive`` is
        False when nobody can answer a step-up challenge: STEP_UP then becomes REVIEW.
        """
        if command.amount_minor > self.settings.max_transfer_minor:
            raise LimitExceededError("amount exceeds the per-transfer limit")
        ip = meta.ip if meta is not None else None
        request_id = meta.request_id if meta is not None else None

        self._lock_user(user_id)
        request_hash = self._request_hash(user_id, command)
        idem_id, replay = self._begin_idempotent(user_id, idempotency_key, request_hash)
        if replay is not None:
            return TransferExecution(replay, self._replayed_transaction(idem_id), True)

        source = get_owned_account(self.session, customer.id, command.from_account_id)
        if source.currency != command.currency:
            raise UnprocessableError("currency does not match the source account")
        destination_id, payee = self._resolve_destination(customer, command, source)

        transaction = ledger.create_transaction(
            self.session,
            type_=TransactionType.TRANSFER,
            amount=command.amount_minor,
            currency=command.currency,
            debit_account_id=source.id,
            credit_account_id=destination_id,
            initiated_by_user_id=user_id,
            payee_id=payee.id if payee else None,
            reference=command.reference,
            channel=channel.value,
        )

        # The scorer contract only knows WEB/MOBILE/API (changing it needs an ADR), so the
        # context keeps its default channel; the real channel is on the transaction and audit row.
        result = self.scorer.score(
            TransactionContext(
                transaction_id=transaction.id,
                customer_id=customer.id,
                account_id=source.id,
                amount_minor=command.amount_minor,
                currency=command.currency,
                occurred_at=utcnow(),
                payee_id=payee.id if payee else None,
                device_id=device_id,
                ip_country=self.countries.country(ip),
                ip_network=ip_network(ip),
            )
        )
        FRAUD_INFERENCE_SECONDS.observe(result.latency_ms / 1000)
        FRAUD_DECISIONS_TOTAL.labels(result.decision.value).inc()
        event = fraud_service.record_event(self.session, transaction.id, customer.id, result)

        decision = result.decision
        user = self.session.get(User, user_id)
        if decision == FraudDecision.STEP_UP and (
            not interactive or user is None or not user.mfa_enabled
        ):
            # cannot step up without a person and a second factor: hold for review
            decision = FraudDecision.REVIEW

        if decision == FraudDecision.ALLOW:
            self._enforce_daily_limit(user_id, transaction)
            ledger.post_transaction(self.session, transaction, ledger.transfer_legs(transaction))
            self._enqueue(notifications.TRANSFER_POSTED, user_id, transaction)
        elif decision == FraudDecision.STEP_UP:
            transaction.status = TransactionStatus.PENDING_STEP_UP
        elif decision == FraudDecision.REVIEW:
            self._enforce_daily_limit(user_id, transaction)
            ledger.place_hold(self.session, transaction)
            transaction.status = TransactionStatus.PENDING_REVIEW
            fraud_service.open_alert(self.session, event)
            self._enqueue(notifications.TRANSFER_UNDER_REVIEW, user_id, transaction)
        else:  # BLOCK
            transaction.status = TransactionStatus.DECLINED
            transaction.decline_reason = "FRAUD_BLOCK"
            fraud_service.open_alert(self.session, event)
            self._enqueue(notifications.TRANSFER_DECLINED, user_id, transaction)

        outcome = self._build_result(transaction, created=True)
        self._store_idempotent_response(idem_id, outcome, transaction.id)
        self.audit.record(
            user_id,
            "TRANSFER_CREATED",
            "transaction",
            str(transaction.id),
            ip=ip,
            request_id=request_id,
            details={
                "status": transaction.status.value,
                "decision": result.decision.value,
                "risk_score": result.risk_score,
                "amount_minor": transaction.amount,
                "channel": channel.value,
            },
        )
        return TransferExecution(outcome, transaction, False)

    def _replayed_transaction(self, idem_id: UUID) -> Transaction:
        row = self.session.get(IdempotencyKey, idem_id)
        transaction = (
            self.session.get(Transaction, row.transaction_id)
            if row is not None and row.transaction_id is not None
            else None
        )
        if transaction is None:
            raise RuntimeError("idempotent replay without its transaction")
        return transaction

    # ---------------------------------------------------------------- idempotency
    @staticmethod
    def _request_hash(user_id: UUID, command: TransferCommand) -> str:
        canonical = json.dumps(
            {
                "user": str(user_id),
                "from": str(command.from_account_id),
                "amount": command.amount_minor,
                "currency": command.currency,
                "payee": str(command.payee_id) if command.payee_id else None,
                "to": str(command.to_account_id) if command.to_account_id else None,
                "reference": command.reference,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _begin_idempotent(
        self, user_id: UUID, key: str, request_hash: str
    ) -> tuple[UUID, TransferResult | None]:
        """Claim the key. A concurrent duplicate BLOCKS on the unique index until we finish."""
        statement = (
            pg_insert(IdempotencyKey)
            .values(
                id=uuid4(),
                user_id=user_id,
                key=key,
                request_hash=request_hash,
                expires_at=utcnow() + timedelta(hours=self.settings.idempotency_ttl_hours),
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_keys_user_key")
            .returning(IdempotencyKey.id)
        )
        inserted = self.session.execute(statement).scalar_one_or_none()
        if inserted is not None:
            return inserted, None
        existing = self.session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
            )
        ).scalar_one()
        if existing.request_hash != request_hash:
            raise IdempotencyKeyReuseError(
                "Idempotency-Key was already used with a different request"
            )
        if existing.response_status is None or existing.response_body is None:
            raise ConflictError("a request with this Idempotency-Key is still in progress")
        IDEMPOTENT_REPLAYS_TOTAL.inc()
        return existing.id, TransferResult(existing.response_status, existing.response_body, True)

    def _store_idempotent_response(
        self, idem_id: UUID, outcome: TransferResult, transaction_id: UUID
    ) -> None:
        row = self.session.get(IdempotencyKey, idem_id)
        if row is None:
            raise RuntimeError("idempotency row vanished")
        row.response_status = outcome.status_code
        row.response_body = outcome.body
        row.transaction_id = transaction_id

    # ------------------------------------------------------------------ helpers
    def _resolve_destination(
        self, customer: Customer, command: TransferCommand, source: Account
    ) -> tuple[UUID, Payee | None]:
        if command.to_account_id is not None:
            own = get_owned_account(self.session, customer.id, command.to_account_id)
            if own.id == source.id:
                raise UnprocessableError("source and destination must differ")
            return own.id, None
        assert command.payee_id is not None
        payee = get_owned_payee(self.session, customer.id, command.payee_id)
        if payee.status != PayeeStatus.VERIFIED:
            raise UnprocessableError("payee must be verified before sending money")
        if payee.bank_code != self.settings.internal_bank_code:
            return ledger.SYSTEM_CLEARING_ID, payee  # external rail is simulated via clearing
        destination = self.session.execute(
            select(Account.id).where(
                Account.account_number == payee.account_number,
                Account.account_type != AccountType.SYSTEM,
            )
        ).scalar_one_or_none()
        if destination is None:
            raise UnprocessableError("payee account is unavailable")
        if destination == source.id:
            raise UnprocessableError("source and destination must differ")
        return destination, payee

    def _lock_user(self, user_id: UUID) -> None:
        """Serialise one user's money movements. ALWAYS the first lock taken in a request
        (before idempotency, account and transaction locks) so lock order is globally consistent."""
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:u))"), {"u": str(user_id)}
        )

    def _enforce_daily_limit(self, user_id: UUID, transaction: Transaction) -> None:
        start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        used = self.session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.initiated_by_user_id == user_id,
                # bill payments count too: the daily limit caps all money a customer sends
                Transaction.type.in_((TransactionType.TRANSFER, TransactionType.BILL_PAYMENT)),
                Transaction.status.in_(_PENDING_OR_DONE),
                Transaction.created_at >= start_of_day,
                Transaction.id != transaction.id,
            )
        ).scalar_one()
        if used + transaction.amount > self.settings.daily_transfer_limit_minor:
            raise LimitExceededError("daily transfer limit exceeded")

    def _enqueue(self, event_type: str, user_id: UUID, transaction: Transaction) -> None:
        notifications.enqueue(
            self.session,
            event_type,
            {
                "user_id": str(user_id),
                "transaction_id": str(transaction.id),
                "account_id": str(transaction.debit_account_id),
                "amount_minor": transaction.amount,
                "currency": transaction.currency,
            },
        )

    @staticmethod
    def _build_result(transaction: Transaction, created: bool = False) -> TransferResult:
        messages = {
            TransactionStatus.POSTED: (201, "Transfer completed"),
            TransactionStatus.PENDING_STEP_UP: (202, "Additional verification is required"),
            TransactionStatus.PENDING_REVIEW: (
                202,
                "Your transfer is being reviewed for your protection",
            ),
            TransactionStatus.DECLINED: (
                403,
                "We could not process this transfer. Please contact support",
            ),
        }
        status_code, message = messages[transaction.status]
        if not created and transaction.status != TransactionStatus.DECLINED:
            status_code = 200  # reads and step-up completions are not 'creations'
        body = {
            "id": str(transaction.id),
            "status": transaction.status.value,
            "amount_minor": transaction.amount,
            "currency": transaction.currency,
            "from_account_id": str(transaction.debit_account_id)
            if transaction.debit_account_id
            else None,
            "to_account_id": None,
            "payee_id": str(transaction.payee_id) if transaction.payee_id else None,
            "reference": transaction.reference,
            "created_at": (transaction.created_at or utcnow()).isoformat(),
            "posted_at": transaction.posted_at.isoformat() if transaction.posted_at else None,
            "step_up_required": transaction.status == TransactionStatus.PENDING_STEP_UP,
            "message": message,
        }
        return TransferResult(status_code, body)

    # --------------------------------------------------------- read / step-up
    def _owned_transaction(
        self, user_id: UUID, transaction_id: UUID, lock: bool = False
    ) -> Transaction:
        stmt = select(Transaction).where(
            Transaction.id == transaction_id, Transaction.initiated_by_user_id == user_id
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        transaction = self.session.execute(stmt).scalar_one_or_none()
        if transaction is None:
            raise NotFoundError("transfer not found")
        return transaction

    def get_transfer(self, user_id: UUID, transaction_id: UUID) -> TransferResult:
        return self._build_result(self._owned_transaction(user_id, transaction_id))

    def confirm_step_up(
        self, *, user_id: UUID, transaction_id: UUID, code: str, meta: RequestMeta
    ) -> TransferResult:
        self._lock_user(user_id)
        transaction = self._owned_transaction(user_id, transaction_id, lock=True)
        if transaction.status != TransactionStatus.PENDING_STEP_UP:
            raise ConflictError("this transfer is not waiting for verification")
        self.auth.verify_step_up(user_id, code, meta)  # commits failure counters itself
        self._enforce_daily_limit(user_id, transaction)
        ledger.post_transaction(self.session, transaction, ledger.transfer_legs(transaction))
        self._enqueue(notifications.TRANSFER_POSTED, user_id, transaction)
        self.audit.record(
            user_id,
            "TRANSFER_STEP_UP_CONFIRMED",
            "transaction",
            str(transaction.id),
            ip=meta.ip,
            request_id=meta.request_id,
        )
        self.session.commit()
        TRANSFERS_TOTAL.labels(transaction.status.value).inc()
        return self._build_result(transaction)

    # --------------------------------------------------------- analyst review
    def review_alert(
        self,
        *,
        analyst_id: UUID,
        alert_id: UUID,
        release: bool,
        note: str | None,
        meta: RequestMeta,
        settle_other: Callable[[Session, Transaction], bool] | None = None,
    ) -> None:
        """Resolve an alert. ``settle_other`` lets another module (bill payments) mirror the
        outcome on its own record in this same transaction; it returns True if it did."""
        alert = self.session.execute(
            select(FraudAlert)
            .where(FraudAlert.id == alert_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if alert is None:
            raise NotFoundError("alert not found")
        if alert.status != AlertStatus.OPEN:
            raise ConflictError("alert already resolved")
        transaction = self.session.execute(
            select(Transaction)
            .where(Transaction.id == alert.transaction_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

        if transaction.status == TransactionStatus.PENDING_REVIEW:
            if release:
                ledger.capture_hold_and_post(self.session, transaction)
            else:
                ledger.release_hold(self.session, transaction)
                transaction.status = TransactionStatus.DECLINED
                transaction.decline_reason = "ANALYST_REJECTED"
        # Engine-blocked (DECLINED) transactions have no hold: the analyst only labels the outcome.
        alert.status = AlertStatus.FALSE_POSITIVE if release else AlertStatus.CONFIRMED_FRAUD
        alert.resolution_note = note
        alert.assigned_to_user_id = analyst_id
        alert.resolved_at = utcnow()

        handled = settle_other is not None and settle_other(self.session, transaction)
        if transaction.initiated_by_user_id is not None and not handled:
            self._enqueue(
                notifications.TRANSFER_POSTED
                if transaction.status == TransactionStatus.POSTED
                else notifications.TRANSFER_DECLINED,
                transaction.initiated_by_user_id,
                transaction,
            )
        self.audit.record(
            analyst_id,
            "FRAUD_ALERT_RESOLVED",
            "fraud_alert",
            str(alert.id),
            ip=meta.ip,
            request_id=meta.request_id,
            details={"release": release, "transaction_id": str(transaction.id)},
        )
        self.session.commit()

    # --------------------------------------------------- scheduled transfers
    def create_scheduled_transfer(
        self,
        *,
        customer: Customer,
        user_id: UUID,
        command: ScheduledTransferCreate,
        meta: RequestMeta | None = None,
    ) -> ScheduledTransfer:
        """Create a scheduled transfer instruction (ADR-0021). Moves no money."""
        today = utcnow().date()
        if command.start_date < today:
            raise UnprocessableError("start_date cannot be in the past")
        if command.amount_minor > self.settings.max_transfer_minor:
            raise LimitExceededError("amount exceeds the per-transfer limit")

        source = get_owned_account(self.session, customer.id, command.from_account_id)
        if source.status != AccountStatus.ACTIVE:
            raise UnprocessableError("source account is not active")
        if source.currency != command.currency:
            raise UnprocessableError("currency does not match the source account")

        cmd = TransferCommand(
            from_account_id=command.from_account_id,
            amount_minor=command.amount_minor,
            currency=command.currency,
            payee_id=command.payee_id,
            to_account_id=command.to_account_id,
            reference=command.reference,
        )
        self._resolve_destination(customer, cmd, source)

        instruction = ScheduledTransfer(
            customer_id=customer.id,
            user_id=user_id,
            from_account_id=command.from_account_id,
            payee_id=command.payee_id,
            to_account_id=command.to_account_id,
            amount=command.amount_minor,
            currency=command.currency,
            reference=command.reference,
            frequency=command.frequency,
            status=ScheduledTransferStatus.ACTIVE,
            start_date=command.start_date,
            end_date=command.end_date,
            next_run_date=command.start_date,
            pause_reason=None,
            failure_count=0,
            last_failure_reason=None,
            last_failed_occurrence_date=None,
            last_run_date=None,
            last_transaction_id=None,
        )
        self.session.add(instruction)
        self.session.flush()

        ip = meta.ip if meta is not None else None
        request_id = meta.request_id if meta is not None else None
        self.audit.record(
            user_id,
            "SCHEDULED_TRANSFER_CREATED",
            "scheduled_transfer",
            str(instruction.id),
            ip=ip,
            request_id=request_id,
            details={
                "from_account_id": str(instruction.from_account_id),
                "amount_minor": instruction.amount,
                "currency": instruction.currency,
                "frequency": instruction.frequency.value,
                "start_date": instruction.start_date.isoformat(),
                "end_date": instruction.end_date.isoformat() if instruction.end_date else None,
            },
        )
        self.session.commit()
        return instruction

    def list_scheduled_transfers(
        self,
        customer_id: UUID,
        *,
        status: ScheduledTransferStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ScheduledTransfer]:
        """List instructions owned by authenticated customer (BOLA-safe)."""
        query = select(ScheduledTransfer).where(ScheduledTransfer.customer_id == customer_id)
        if status is not None:
            query = query.where(ScheduledTransfer.status == status)
        query = query.order_by(ScheduledTransfer.created_at.desc(), ScheduledTransfer.id.desc())
        query = query.limit(limit).offset(offset)
        return list(self.session.execute(query).scalars().all())

    def get_scheduled_transfer(
        self, customer_id: UUID, scheduled_transfer_id: UUID
    ) -> ScheduledTransfer:
        """Fetch a single owned instruction (404 for non-owned or non-existent)."""
        return get_owned_scheduled_transfer(self.session, customer_id, scheduled_transfer_id)

    def cancel_scheduled_transfer(
        self,
        *,
        customer_id: UUID,
        user_id: UUID,
        scheduled_transfer_id: UUID,
        meta: RequestMeta | None = None,
    ) -> ScheduledTransfer:
        """Cancel an owned scheduled transfer (terminal transition)."""
        self._lock_user(user_id)
        instruction = get_owned_scheduled_transfer(
            self.session, customer_id, scheduled_transfer_id, for_update=True
        )
        if instruction.status == ScheduledTransferStatus.CANCELLED:
            raise ConflictError("scheduled transfer is already cancelled")
        if instruction.status == ScheduledTransferStatus.COMPLETED:
            raise ConflictError("completed scheduled transfer cannot be cancelled")

        prev_status = instruction.status.value
        instruction.status = ScheduledTransferStatus.CANCELLED
        instruction.pause_reason = None

        ip = meta.ip if meta is not None else None
        request_id = meta.request_id if meta is not None else None
        self.audit.record(
            user_id,
            "SCHEDULED_TRANSFER_CANCELLED",
            "scheduled_transfer",
            str(instruction.id),
            ip=ip,
            request_id=request_id,
            details={"previous_status": prev_status},
        )
        self.session.commit()
        return instruction

    def pause_scheduled_transfer(
        self,
        *,
        customer_id: UUID,
        user_id: UUID,
        scheduled_transfer_id: UUID,
        meta: RequestMeta | None = None,
    ) -> ScheduledTransfer:
        """Customer-initiated pause on an active instruction."""
        self._lock_user(user_id)
        instruction = get_owned_scheduled_transfer(
            self.session, customer_id, scheduled_transfer_id, for_update=True
        )
        if instruction.status == ScheduledTransferStatus.PAUSED:
            raise ConflictError("scheduled transfer is already paused")
        if instruction.status == ScheduledTransferStatus.CANCELLED:
            raise ConflictError("cancelled scheduled transfer cannot be paused")
        if instruction.status == ScheduledTransferStatus.COMPLETED:
            raise ConflictError("completed scheduled transfer cannot be paused")

        instruction.status = ScheduledTransferStatus.PAUSED
        instruction.pause_reason = SchedulePauseReason.CUSTOMER

        ip = meta.ip if meta is not None else None
        request_id = meta.request_id if meta is not None else None
        self.audit.record(
            user_id,
            "SCHEDULED_TRANSFER_PAUSED",
            "scheduled_transfer",
            str(instruction.id),
            ip=ip,
            request_id=request_id,
            details={"reason": "customer"},
        )
        self.session.commit()
        return instruction

    def resume_scheduled_transfer(
        self,
        *,
        customer: Customer,
        user_id: UUID,
        scheduled_transfer_id: UUID,
        meta: RequestMeta | None = None,
    ) -> ScheduledTransfer:
        """Customer-initiated resume on a paused instruction (R1/R4/R5)."""
        self._lock_user(user_id)
        instruction = get_owned_scheduled_transfer(
            self.session, customer.id, scheduled_transfer_id, for_update=True
        )
        if instruction.status == ScheduledTransferStatus.ACTIVE:
            raise ConflictError("scheduled transfer is already active")
        if instruction.status == ScheduledTransferStatus.CANCELLED:
            raise ConflictError("cancelled scheduled transfer cannot be resumed")
        if instruction.status == ScheduledTransferStatus.COMPLETED:
            raise ConflictError("completed scheduled transfer cannot be resumed")
        if instruction.pause_reason == SchedulePauseReason.FRAUD_BLOCK:
            raise ConflictError(
                "Scheduled transfer is locked following a security decline "
                "and requires fraud analyst clearance"
            )

        source = get_owned_account(self.session, customer.id, instruction.from_account_id)
        if source.status != AccountStatus.ACTIVE:
            raise UnprocessableError("source account is not active")

        cmd = TransferCommand(
            from_account_id=instruction.from_account_id,
            amount_minor=instruction.amount,
            currency=instruction.currency,
            payee_id=instruction.payee_id,
            to_account_id=instruction.to_account_id,
            reference=instruction.reference,
        )
        self._resolve_destination(customer, cmd, source)

        today = utcnow().date()
        next_run: date | None = instruction.next_run_date
        while next_run is not None and next_run < today:
            next_run = compute_next_occurrence(
                instruction.start_date,
                next_run,
                instruction.frequency,
                instruction.end_date,
            )

        if next_run is None or (
            instruction.end_date is not None and next_run > instruction.end_date
        ):
            instruction.status = ScheduledTransferStatus.COMPLETED
        else:
            instruction.status = ScheduledTransferStatus.ACTIVE
            instruction.next_run_date = next_run

        instruction.pause_reason = None
        instruction.failure_count = 0
        instruction.last_failure_reason = None

        ip = meta.ip if meta is not None else None
        request_id = meta.request_id if meta is not None else None
        self.audit.record(
            user_id,
            "SCHEDULED_TRANSFER_RESUMED",
            "scheduled_transfer",
            str(instruction.id),
            ip=ip,
            request_id=request_id,
            details={
                "next_run_date": instruction.next_run_date.isoformat(),
                "status": instruction.status.value,
            },
        )
        self.session.commit()
        return instruction

    # ------------------------------------------------------------- projections
    def upcoming_occurrences(self, customer_id: UUID, until: date) -> list[UpcomingOccurrence]:
        """Project upcoming occurrences for customer's active instructions (R15)."""
        from pbip_api.modules.payments.recurrence import upcoming_occurrences

        return upcoming_occurrences(self.session, customer_id, until)
