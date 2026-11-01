"""The hybrid fraud scorer on the request path (TASK-033, ADR-0019).

``HybridFraudScorer`` implements the ``FraudScorer`` contract for transfers and bill payments:

1. The baseline rules score the transfer first. They are the bank's policy and the fail-safe.
2. The platform's own records give the model its inputs: debits in the last 30 days, payee age,
   failed sign-ins in 24 hours and the device. The ``pbip_ml`` online features turn them into the
   row the models were trained on.
3. The models score that row in a worker thread, bounded by ``FRAUD_MODEL_BUDGET_MS``.
4. The higher of the two risks decides, so the model can raise a decision but never lower one
   the rules reached. Reasons are merged by code, and the snapshot keeps both scores.

Any model problem (not loaded, raises, too slow) returns the baseline result, marked
``+rules-only`` with a ``MODEL_UNAVAILABLE`` reason. The result is never ALLOW-all.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from pbip_ml.fraud.contract import FraudReason, FraudScoreResult, ReasonSource, TransactionContext
from pbip_ml.fraud.features import AVG_WINDOW, PastTransfer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.accounts.models import AccountHolder
from pbip_api.modules.auth.models import SecurityEvent, SecurityEventType
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud.baseline import BaselineRulesScorer, decide
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.ledger.models import Transaction
from pbip_api.modules.payees.models import Payee

if TYPE_CHECKING:  # the model stack (LightGBM, scikit-learn) loads only when the flag is on
    from pbip_ml.fraud.hybrid import HybridScorer
    from pbip_ml.fraud.inference import TransferInputs

_logger = structlog.get_logger(__name__)

LOGIN_WINDOW = timedelta(hours=24)
RULES_ONLY_SUFFIX = "+rules-only"
# Model scoring is CPU-bound and short. Two workers are enough for a bounded wait, and a timed-out
# call finishes in the background without blocking the request.
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fraud-model")


def load_engine(settings: Settings) -> HybridScorer | None:
    """Called once when the app is created. ``None`` means the rules decide alone.

    Attempts to load the model dynamically from the MLflow Model Registry using the
    champion alias (TASK-038). If the MLflow server is disconnected or unreachable,
    it falls back to loading from local disk (settings.fraud_model_dir).
    If both fail or are unconfigured, returns None (rules-only fail-safe, never crashes).
    """
    if settings.fraud_scorer != "hybrid":
        return None

    engine: HybridScorer | None = None

    # Step 1: Attempt dynamic load from MLflow Model Registry ('champion' alias)
    if settings.fraud_registered_model_name and settings.mlflow_tracking_uri:
        try:
            from pbip_ml.fraud.inference import load_from_registry

            cache_path = (
                Path(settings.fraud_model_cache_dir) if settings.fraud_model_cache_dir else None
            )
            engine = load_from_registry(
                registry_name=settings.fraud_registered_model_name,
                alias=settings.fraud_model_alias,
                tracking_uri=settings.mlflow_tracking_uri,
                cache_dir=cache_path,
            )
            _logger.info(
                "fraud_model_loaded_from_registry",
                registry=settings.fraud_registered_model_name,
                alias=settings.fraud_model_alias,
                model_version=engine.params.model_version,
            )
        except Exception as exc:
            _logger.warning(
                "fraud_model_registry_fallback",
                reason=type(exc).__name__,
                detail=str(exc)[:200],
            )
            engine = None

    # Step 2: Fallback to local disk if registry load failed or was not available
    if engine is None and settings.fraud_model_dir:
        try:
            from pbip_ml.fraud.inference import load

            engine = load(Path(settings.fraud_model_dir))
            _logger.info(
                "fraud_model_loaded_from_disk_fallback",
                model_version=engine.params.model_version,
                path=settings.fraud_model_dir,
            )
        except Exception as exc:
            _logger.error(
                "fraud_model_not_loaded",
                reason=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return None

    if engine is None:
        _logger.error(
            "fraud_model_not_loaded",
            reason="Neither MLflow registry champion nor valid local disk fallback found",
        )
        return None

    # Step 3: Warm-up score
    try:
        from pbip_ml.fraud.inference import TransferInputs, score_transfer

        warm_up = TransferInputs(
            100_00, "INR", datetime(2026, 1, 1, tzinfo=UTC), None, None, False, 0, ()
        )
        score_transfer(engine, warm_up)
    except Exception as exc:
        _logger.error("fraud_model_not_loaded", reason=type(exc).__name__, detail=str(exc)[:200])
        return None

    _logger.info("fraud_model_ready", model_version=engine.params.model_version)
    return engine


class HybridFraudScorer:
    def __init__(
        self,
        session: Session,
        engine: HybridScorer | None,
        budget_ms: int,
        counters: VelocityCounters | None = None,
    ) -> None:
        self.session = session
        self.engine = engine
        self.budget_seconds = budget_ms / 1000
        self.baseline = BaselineRulesScorer(session, counters)

    def score(self, context: TransactionContext) -> FraudScoreResult:
        started = time.perf_counter()
        base = self.baseline.score(context)
        if self.engine is None:
            return _rules_only(base, "NOT_LOADED", started)
        from pbip_ml.fraud.inference import score_transfer  # loaded already by load_engine

        inputs = self._inputs(context, new_device=bool(base.features.get("new_device")))
        try:
            hybrid = _POOL.submit(score_transfer, self.engine, inputs).result(
                timeout=self.budget_seconds
            )
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

    def _inputs(self, context: TransactionContext, new_device: bool) -> TransferInputs:
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
        debits = self.session.execute(
            select(
                Transaction.created_at,
                Transaction.amount,
                FraudEvent.features["device_id"].astext,  # the device of each earlier transfer
            )
            .outerjoin(FraudEvent, FraudEvent.transaction_id == Transaction.id)
            .where(
                Transaction.debit_account_id.in_(accounts),
                Transaction.id != context.transaction_id,
                Transaction.created_at >= at - AVG_WINDOW,
                Transaction.created_at < at,
            )
        ).all()
        history = [
            PastTransfer(created, "DEBIT", amount, UUID(device) if device else None)
            for created, amount, device in debits
        ]
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
