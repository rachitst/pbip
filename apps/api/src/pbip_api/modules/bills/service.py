"""Bill payment use cases: biller lookup, validation, idempotency, double-entry ledger posting.

Transaction boundary: ONE database transaction per request. It contains the per-user advisory lock,
the idempotency row, the ledger transaction header, the balanced debit/credit entries, the bill
payment row, the audit log entry and the outbox notification event. Any exception before ``commit``
rolls back the entire transaction so retries with the same ``Idempotency-Key`` remain safe.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
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
)
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.accounts.policy import get_owned_account
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.bills.models import Biller, BillerStatus, BillPayment, BillPaymentStatus
from pbip_api.modules.bills.policy import get_owned_bill_payment
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud import service as fraud_service
from pbip_api.modules.fraud.signals import ip_network
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    EntryDirection,
    IdempotencyKey,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.payments.service import IdempotencyKeyReuseError, LimitExceededError

_COUNTS_TOWARDS_LIMIT = (
    TransactionStatus.POSTED,
    TransactionStatus.PENDING_REVIEW,
    TransactionStatus.PENDING_STEP_UP,
)


class BillerInactiveError(UnprocessableError):
    code = "biller_inactive"


@dataclass(frozen=True, slots=True)
class PayBillCommand:
    from_account_id: UUID
    biller_id: UUID
    consumer_reference: str
    amount_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class BillPaymentResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


class BillsService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        scorer: FraudScorer,
        countries: CountryResolver | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.scorer = scorer
        self.countries = countries or NoCountryResolver()
        self.audit = AuditService(session)

    # ------------------------------------------------------------------ queries
    def list_billers(self, *, active_only: bool = True) -> list[Biller]:
        stmt = select(Biller).order_by(Biller.name.asc())
        if active_only:
            stmt = stmt.where(Biller.status == BillerStatus.ACTIVE)
        return list(self.session.execute(stmt).scalars().all())

    def get_bill_payment(self, customer_id: UUID, bill_payment_id: UUID) -> BillPaymentResult:
        payment = get_owned_bill_payment(self.session, customer_id, bill_payment_id)
        biller = self.session.get(Biller, payment.biller_id)
        if biller is None:
            raise NotFoundError("biller not found")
        reference, new_balance_minor = self._resolve_ledger_context(payment, biller)
        return self._build_result(
            payment,
            biller=biller,
            reference=reference,
            new_balance_minor=new_balance_minor,
            created=False,
        )

    def list_bill_payments(self, customer_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(BillPayment, Biller)
            .join(Biller, Biller.id == BillPayment.biller_id)
            .where(BillPayment.customer_id == customer_id)
            .order_by(BillPayment.created_at.desc(), BillPayment.id.desc())
            .limit(limit)
        ).all()
        results: list[dict[str, Any]] = []
        for payment, biller in rows:
            reference, new_balance_minor = self._resolve_ledger_context(payment, biller)
            results.append(
                self._build_result(
                    payment,
                    biller=biller,
                    reference=reference,
                    new_balance_minor=new_balance_minor,
                    created=False,
                ).body
            )
        return results

    # ----------------------------------------------------------------- payment
    def pay_bill(
        self,
        *,
        user_id: UUID,
        customer: Customer,
        command: PayBillCommand,
        idempotency_key: str,
        meta: RequestMeta,
        device_id: UUID | None = None,
    ) -> BillPaymentResult:
        if command.amount_minor > self.settings.max_transfer_minor:
            raise LimitExceededError("amount exceeds the per-payment limit")
        self._lock_user(user_id)
        request_hash = self._request_hash(user_id, command)
        idem_id, replay = self._begin_idempotent(user_id, idempotency_key, request_hash)
        if replay is not None:
            return replay

        source = get_owned_account(self.session, customer.id, command.from_account_id)
        if source.currency != command.currency:
            raise UnprocessableError("currency does not match the source account")

        biller = self._get_active_biller(command.biller_id)
        reference = f"BILL/{biller.biller_code}/{command.consumer_reference}"[:140]

        transaction = ledger.create_transaction(
            self.session,
            type_=TransactionType.BILL_PAYMENT,
            amount=command.amount_minor,
            currency=command.currency,
            debit_account_id=source.id,
            credit_account_id=ledger.SYSTEM_CLEARING_ID,
            initiated_by_user_id=user_id,
            reference=reference,
        )
        # Same fraud contract as transfers. Bill payments are not held for review: anything but
        # ALLOW declines the payment (fail safe) and opens an alert for an analyst.
        result = self.scorer.score(
            TransactionContext(
                transaction_id=transaction.id,
                customer_id=customer.id,
                account_id=source.id,
                amount_minor=command.amount_minor,
                currency=command.currency,
                occurred_at=utcnow(),
                payee_id=None,
                device_id=device_id,
                ip_country=self.countries.country(meta.ip),
                ip_network=ip_network(meta.ip),
            )
        )
        FRAUD_INFERENCE_SECONDS.observe(result.latency_ms / 1000)
        FRAUD_DECISIONS_TOTAL.labels(result.decision.value).inc()
        event = fraud_service.record_event(self.session, transaction.id, customer.id, result)
        if result.decision == FraudDecision.ALLOW:
            self._enforce_daily_limit(user_id, transaction)
            ledger.post_transaction(self.session, transaction, ledger.transfer_legs(transaction))
            status = BillPaymentStatus.POSTED
        else:
            transaction.status = TransactionStatus.DECLINED
            transaction.decline_reason = "FRAUD_BLOCK"
            fraud_service.open_alert(self.session, event)
            status = BillPaymentStatus.DECLINED

        payment = BillPayment(
            customer_id=customer.id,
            account_id=source.id,
            biller_id=biller.id,
            transaction_id=transaction.id,
            consumer_reference=command.consumer_reference,
            amount=command.amount_minor,
            currency=command.currency,
            status=status,
        )
        self.session.add(payment)
        self.session.flush()

        self._enqueue(user_id, transaction, biller, payment)
        outcome = self._build_result(
            payment,
            biller=biller,
            reference=reference,
            new_balance_minor=source.ledger_balance,
            created=True,
        )
        self._store_idempotent_response(idem_id, outcome, transaction.id)
        self.audit.record(
            user_id,
            "BILL_PAYMENT_CREATED",
            "bill_payment",
            str(payment.id),
            ip=meta.ip,
            request_id=meta.request_id,
            details={
                "transaction_id": str(transaction.id),
                "biller_id": str(biller.id),
                "biller_code": biller.biller_code,
                "amount_minor": payment.amount,
                "status": payment.status.value,
            },
        )
        self.session.commit()
        return outcome

    # ------------------------------------------------------------- idempotency
    @staticmethod
    def _request_hash(user_id: UUID, command: PayBillCommand) -> str:
        canonical = json.dumps(
            {
                "kind": "bill_payment",
                "user": str(user_id),
                "from": str(command.from_account_id),
                "biller": str(command.biller_id),
                "consumer_reference": command.consumer_reference,
                "amount": command.amount_minor,
                "currency": command.currency,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _begin_idempotent(
        self, user_id: UUID, key: str, request_hash: str
    ) -> tuple[UUID, BillPaymentResult | None]:
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
        return existing.id, BillPaymentResult(
            existing.response_status, existing.response_body, True
        )

    def _store_idempotent_response(
        self, idem_id: UUID, outcome: BillPaymentResult, transaction_id: UUID
    ) -> None:
        row = self.session.get(IdempotencyKey, idem_id)
        if row is None:
            raise RuntimeError("idempotency row vanished")
        row.response_status = outcome.status_code
        row.response_body = outcome.body
        row.transaction_id = transaction_id

    # ----------------------------------------------------------------- helpers
    def _get_active_biller(self, biller_id: UUID) -> Biller:
        biller = self.session.get(Biller, biller_id)
        if biller is None:
            raise NotFoundError("biller not found")
        if biller.status != BillerStatus.ACTIVE:
            raise BillerInactiveError("biller is not currently accepting payments")
        return biller

    def _enforce_daily_limit(self, user_id: UUID, transaction: Transaction) -> None:
        """Transfers and bill payments share one daily limit (all money the customer sends)."""
        start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        used = self.session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.initiated_by_user_id == user_id,
                Transaction.type.in_((TransactionType.TRANSFER, TransactionType.BILL_PAYMENT)),
                Transaction.status.in_(_COUNTS_TOWARDS_LIMIT),
                Transaction.created_at >= start_of_day,
                Transaction.id != transaction.id,
            )
        ).scalar_one()
        if used + transaction.amount > self.settings.daily_transfer_limit_minor:
            raise LimitExceededError("daily payment limit exceeded")

    def _lock_user(self, user_id: UUID) -> None:
        """Serialise one user's money movements. ALWAYS the first lock taken in a request
        (before idempotency, account and transaction locks) so lock order is globally consistent."""
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:u))"), {"u": str(user_id)}
        )

    def _enqueue(
        self,
        user_id: UUID,
        transaction: Transaction,
        biller: Biller,
        payment: BillPayment,
    ) -> None:
        posted = payment.status == BillPaymentStatus.POSTED
        notifications.enqueue(
            self.session,
            notifications.BILL_PAYMENT_POSTED if posted else notifications.BILL_PAYMENT_DECLINED,
            {
                "user_id": str(user_id),
                "transaction_id": str(transaction.id),
                "account_id": str(payment.account_id),
                "bill_payment_id": str(payment.id),
                "biller_name": biller.name,
                "amount_minor": payment.amount,
                "currency": payment.currency,
            },
        )

    def _resolve_ledger_context(self, payment: BillPayment, biller: Biller) -> tuple[str, int]:
        default_ref = f"BILL/{biller.biller_code}/{payment.consumer_reference}"[:140]
        txn = self.session.get(Transaction, payment.transaction_id)
        reference = txn.reference if txn is not None and txn.reference else default_ref

        debit_balance = self.session.execute(
            select(TransactionEntry.balance_after).where(
                TransactionEntry.transaction_id == payment.transaction_id,
                TransactionEntry.account_id == payment.account_id,
                TransactionEntry.direction == EntryDirection.DEBIT,
            )
        ).scalar_one_or_none()
        if debit_balance is not None:
            return reference, int(debit_balance)

        account = self.session.get(Account, payment.account_id)
        return reference, int(account.ledger_balance) if account is not None else 0

    @staticmethod
    def _build_result(
        payment: BillPayment,
        *,
        biller: Biller,
        reference: str,
        new_balance_minor: int,
        created: bool = False,
    ) -> BillPaymentResult:
        if not created:
            status_code = 200
        elif payment.status == BillPaymentStatus.DECLINED:
            status_code = 403  # same as a declined transfer: a plain JSON body, not problem+json
        else:
            status_code = 201
        body = {
            "id": str(payment.id),
            "customer_id": str(payment.customer_id),
            "account_id": str(payment.account_id),
            "biller_id": str(payment.biller_id),
            "biller_name": biller.name,
            "biller_code": biller.biller_code,
            "transaction_id": str(payment.transaction_id),
            "consumer_reference": payment.consumer_reference,
            "amount_minor": payment.amount,
            "currency": payment.currency,
            "status": payment.status.value,
            "reference": reference,
            "new_balance_minor": new_balance_minor,
            "created_at": (payment.created_at or utcnow()).isoformat(),
            "message": (
                f"Bill payment to {biller.name} completed"
                if payment.status == BillPaymentStatus.POSTED
                else f"Bill payment to {biller.name} could not be completed"
            ),
        }
        return BillPaymentResult(status_code, body)
