"""The hybrid fraud scorer on the request path (TASK-033, ADR-0019).

``HybridFraudScorer`` implements the ``FraudScorer`` contract for transfers and bill payments:

1. The baseline rules score the transfer first. They are the bank's policy and the fail-safe.
2. The platform's own records give the model its inputs: debits and received money in the last
   30 days, the last customer activity before that if there was none since, payee age, failed
   sign-ins in 24 hours and the device. The ``pbip_ml`` online features turn them into the row
   the models were trained on (``test_fraud_feature_parity`` proves the same numbers as offline).
3. The models score that row in a worker thread, bounded by ``FRAUD_MODEL_BUDGET_MS``.
4. The higher of the two risks decides, so the model can raise a decision but never lower one
   the rules reached. Reasons are merged by code, and the snapshot keeps both scores.

Any model problem (not loaded, raises, too slow) returns the baseline result, marked
``+rules-only`` with a ``MODEL_UNAVAILABLE`` reason. The result is never ALLOW-all.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import structlog
from pbip_ml.fraud.contract import FraudReason, FraudScoreResult, ReasonSource, TransactionContext
from pbip_ml.fraud.features import AVG_WINDOW, PastTransfer
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql import Select

from pbip_api.core.config import Settings
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.accounts.models import AccountHolder
from pbip_api.modules.auth.models import SecurityEvent, SecurityEventType
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud.baseline import ACTIVITY_TYPES, BaselineRulesScorer, decide
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.fraud.travel import TravelPolicy
from pbip_api.modules.ledger.models import Transaction, TransactionStatus
from pbip_api.modules.payees.models import Payee

if TYPE_CHECKING:  # the model stack (LightGBM, scikit-learn) loads only when the flag is on
    from pbip_ml.fraud.hybrid import HybridResult, HybridScorer
    from pbip_ml.fraud.inference import TransferInputs

_logger = structlog.get_logger(__name__)

LOGIN_WINDOW = timedelta(hours=24)
# Customer-induced activity: the same set as DORMANT_REACTIVATION (ADR-0046, ADR-0051). Fees,
# interest, payouts, reversals and loan postings neither wake an account nor count as structuring.
_IS_ACTIVITY = and_(
    Transaction.status == TransactionStatus.POSTED, Transaction.type.in_(ACTIVITY_TYPES)
)
RULES_ONLY_SUFFIX = "+rules-only"
# Model scoring is CPU-bound and short. Two workers are enough for a bounded wait, and a timed-out
# call finishes in the background without blocking the request.
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fraud-model")


def load_engine(settings: Settings) -> "HybridScorer | None":
    """Called once when the app is created. ``None`` means the rules decide alone.

    The model stack is imported here, not at module import: with ``FRAUD_SCORER=rules`` an API
    process never loads scikit-learn (``test_training_code_stays_off_the_request_path``).
    """
    if settings.fraud_scorer != "hybrid":
        return None
    if not settings.fraud_model_dir:
        _logger.error("fraud_model_not_loaded", reason="FRAUD_MODEL_DIR is not set")
        return None
    # Anything here may fail: a missing native library (libgomp for LightGBM), a broken file, a
    # bad pickle. The API must still start and serve the rules (fail safe), never crash.
    try:
        from pbip_ml.fraud.inference import TransferInputs, load, score_transfer

        engine = load(Path(settings.fraud_model_dir))
        # One throw-away score warms LightGBM/sklearn/pandas, so the first customer's transfer
        # is not the one that pays the cold start (34 ms vs a 15 ms median on the laptop).
        warm_up = TransferInputs(
            100_00, "INR", datetime(2026, 1, 1, tzinfo=UTC), None, None, False, 0, ()
        )
        score_transfer(engine, warm_up)
    except Exception as exc:  # fail safe: rules only, never a crashed API or ALLOW-all
        _logger.error("fraud_model_not_loaded", reason=type(exc).__name__, detail=str(exc)[:200])
        return None
    _logger.info("fraud_model_loaded", model_version=engine.params.model_version)
    return engine


def score_within_budget(
    engine: "HybridScorer", inputs: "TransferInputs", budget_seconds: float
) -> "HybridResult":
    """The model step alone: score in the worker pool, wait at most the budget.

    Raises ``concurrent.futures.TimeoutError`` past the budget; the call then finishes in the
    background. Shared with ``scripts/benchmark_fraud_latency.py`` so the benchmark measures the
    fallback rate of exactly this code path.
    """
    from pbip_ml.fraud.inference import score_transfer  # loaded already by load_engine

    return _POOL.submit(score_transfer, engine, inputs).result(timeout=budget_seconds)


class HybridFraudScorer:
    def __init__(
        self,
        session: Session,
        engine: "HybridScorer | None",
        budget_ms: int,
        counters: VelocityCounters | None = None,
        travel: TravelPolicy | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.engine = engine
        self.budget_seconds = budget_ms / 1000
        self.baseline = BaselineRulesScorer(session, counters, travel, settings=settings)

    def score(self, context: TransactionContext) -> FraudScoreResult:
        started = time.perf_counter()
        base = self.baseline.score(context)
        if self.engine is None:
            return _rules_only(base, "NOT_LOADED", started)
        inputs = self._inputs(context, new_device=bool(base.features.get("new_device")))
        try:
            hybrid = score_within_budget(self.engine, inputs, self.budget_seconds)
        except FutureTimeout:
            return _rules_only(base, "TIMEOUT", started)
        except Exception as exc:  # any model failure: fail safe to the rules, never ALLOW-all
            return _rules_only(base, type(exc).__name__, started)
        if hybrid.model_version.endswith(RULES_ONLY_SUFFIX):  # the models raised inside
            return _rules_only(base, str(hybrid.features.get("model_error", "MODEL")), started)
        risk = max(base.risk_score, hybrid.risk_score)
        seen = {r.code for r in base.reasons}
        return FraudScoreResult(
            risk_score=risk,
            decision=decide(risk),
            reasons=[*base.reasons, *(r for r in hybrid.reasons if r.code not in seen)],
            model_version=hybrid.model_version,
            latency_ms=_elapsed_ms(started),
            features={
                **base.features,
                **hybrid.features,
                "baseline_risk": base.risk_score,
                "hybrid_risk": hybrid.risk_score,
            },
        )

    def _inputs(self, context: TransactionContext, new_device: bool) -> "TransferInputs":
        from pbip_ml.fraud.inference import TransferInputs

        at = context.occurred_at
        payee_added_at = (
            self.session.scalar(select(Payee.created_at).where(Payee.id == context.payee_id))
            if context.payee_id is not None
            else None
        )
        accounts = select(AccountHolder.account_id).where(
            AccountHolder.customer_id == context.customer_id
        )
        window = (
            Transaction.id != context.transaction_id,
            Transaction.created_at >= at - AVG_WINDOW,
            Transaction.created_at < at,
        )
        debits = self.session.execute(
            select(
                Transaction.created_at,
                Transaction.amount,
                FraudEvent.features["device_id"].astext,  # the device of each earlier transfer
                _IS_ACTIVITY,
            )
            .outerjoin(FraudEvent, FraudEvent.transaction_id == Transaction.id)
            .where(Transaction.debit_account_id.in_(accounts), *window)
        ).all()
        # Money received, from anyone but the customer's own accounts (a move between them is
        # already a debit above). No device: the sender made it (ADR-0051).
        credits = self.session.execute(
            select(Transaction.created_at, Transaction.amount, _IS_ACTIVITY).where(
                Transaction.credit_account_id.in_(accounts),
                or_(
                    Transaction.debit_account_id.is_(None),
                    Transaction.debit_account_id.not_in(accounts),
                ),
                *window,
            )
        ).all()
        history = [
            PastTransfer(created, "DEBIT", amount, UUID(device) if device else None, bool(active))
            for created, amount, device, active in debits
        ] + [
            PastTransfer(created, "CREDIT", amount, None, bool(active))
            for created, amount, active in credits
        ]
        if not any(h.activity for h in history):
            history += self._last_activity_before(context, accounts, at - AVG_WINDOW)
        return TransferInputs(
            amount_minor=context.amount_minor,
            currency=context.currency,
            occurred_at=at,
            payee_added_at=payee_added_at,
            device_id=context.device_id,
            new_device=new_device,  # NEW_DEVICE_OR_NETWORK's device half (TASK-095)
            failed_logins_24h=self._failed_logins(context.customer_id, at - LOGIN_WINDOW),
            history=history,
        )

    def _last_activity_before(
        self, context: TransactionContext, accounts: Select[UUID], before: datetime
    ) -> list[PastTransfer]:
        """The customer's latest activity older than the 30-day history, for
        ``days_since_last_activity`` (F8 dormancy). Only asked when the 30 days hold none, so an
        active customer pays nothing; two index range scans, each stopping at its first row.
        Outside every other feature's window, so only its time matters."""
        sides: tuple[tuple[InstrumentedAttribute[UUID | None], Literal["DEBIT", "CREDIT"]], ...] = (
            (Transaction.debit_account_id, "DEBIT"),
            (Transaction.credit_account_id, "CREDIT"),
        )
        found: list[PastTransfer] = []
        for side, direction in sides:
            row = self.session.execute(
                select(Transaction.created_at, Transaction.amount)
                .where(
                    side.in_(accounts),
                    _IS_ACTIVITY,
                    Transaction.id != context.transaction_id,
                    Transaction.created_at < before,
                )
                .order_by(Transaction.created_at.desc())
                .limit(1)
            ).first()
            if row is not None:
                found.append(PastTransfer(row[0], direction, row[1], None))
        return found

    def _failed_logins(self, customer_id: UUID, since: datetime) -> int:
        user_id = select(Customer.user_id).where(Customer.id == customer_id).scalar_subquery()
        count = self.session.scalar(
            select(func.count()).where(
                SecurityEvent.user_id == user_id,
                SecurityEvent.event_type == SecurityEventType.LOGIN_FAILURE,
                SecurityEvent.created_at >= since,
            )
        )
        return int(count or 0)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _rules_only(base: FraudScoreResult, why: str, started: float) -> FraudScoreResult:
    _logger.warning("fraud_model_fallback", reason=why)
    reason = FraudReason(
        code="MODEL_UNAVAILABLE",
        message="Scored by rules only: the model could not be used",
        source=ReasonSource.RULE,
    )
    return base.model_copy(
        update={
            "reasons": [*base.reasons, reason],
            "model_version": base.model_version + RULES_ONLY_SUFFIX,
            "latency_ms": _elapsed_ms(started),
            "features": {**base.features, "model_error": why[:40]},
        }
    )
