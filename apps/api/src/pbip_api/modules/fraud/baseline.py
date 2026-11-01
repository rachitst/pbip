"""Baseline, fully explainable rules scorer (deterministic stop-gap until the ML engine ships).

Implements the ``FraudScorer`` contract. Every point of score is attributed to a named rule with
a human-readable message, so an analyst can always see WHY a transfer was flagged. The downstream
hybrid engine (rules + LightGBM + Isolation Forest + SHAP) wraps this behind the same
interface; these rules become its deterministic layer.
"""

import time
from datetime import datetime, timedelta

from pbip_ml.fraud.contract import (
    FraudDecision,
    FraudReason,
    FraudScoreResult,
    ReasonSource,
    TransactionContext,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.money import format_minor
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.auth.models import SecurityEvent, SecurityEventType
from pbip_api.modules.customers.models import Customer, Device
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.fraud.signals import device_uuid_from_hash
from pbip_api.modules.ledger.models import Transaction, TransactionStatus, TransactionType
from pbip_api.modules.payees.models import Payee

MODEL_VERSION = "baseline-rules-0.3"  # 0.2: NEW_COUNTRY (ADR-0014); 0.3: TASK-095 signals

NEW_PAYEE_WINDOW = timedelta(hours=24)
NEW_PAYEE_LARGE_MINOR = 2_500_000  # 25,000.00
LARGE_AMOUNT_MINOR = 10_000_000  # 1,00,000.00
SPIKE_MULTIPLIER = 5.0
SPIKE_MIN_HISTORY = 3
VELOCITY_WINDOW = timedelta(minutes=10)
VELOCITY_COUNT = 4
NEW_COUNTRY_POINTS = 20  # a signal, not a decision on its own (below STEP_UP_AT)
COUNTRY_HISTORY_WINDOW = timedelta(days=180)
LOGIN_FAILURE_WINDOW = timedelta(hours=24)
LOGIN_FAILURES_MIN = 3
LOGIN_FAILURES_POINTS = 20  # signals: each alone stays below STEP_UP_AT; together they reach it
NEW_DEVICE_OR_NETWORK_POINTS = 15
VOLUME_WINDOW = timedelta(hours=1)

STEP_UP_AT = 30
REVIEW_AT = 60
BLOCK_AT = 85


def decide(score: int) -> FraudDecision:
    if score >= BLOCK_AT:
        return FraudDecision.BLOCK
    if score >= REVIEW_AT:
        return FraudDecision.REVIEW
    if score >= STEP_UP_AT:
        return FraudDecision.STEP_UP
    return FraudDecision.ALLOW


class BaselineRulesScorer:
    model_version = MODEL_VERSION

    def __init__(self, session: Session, counters: VelocityCounters | None = None) -> None:
        self.session = session
        self.counters = counters  # Redis windows (TASK-034); None or unreachable -> PostgreSQL

    def score(self, context: TransactionContext) -> FraudScoreResult:
        started = time.perf_counter()
        now = utcnow()
        reasons: list[FraudReason] = []
        score = 0

        payee_age_minutes = self._payee_age_minutes(context, now)

        history_avg, history_count = self.session.execute(
            select(func.avg(Transaction.amount), func.count()).where(
                Transaction.debit_account_id == context.account_id,
                Transaction.type == TransactionType.TRANSFER,
                Transaction.status == TransactionStatus.POSTED,
                Transaction.created_at >= now - timedelta(days=90),
            )
        ).one()
        average = float(history_avg) if history_avg is not None else 0.0

        recent_count = self._recent_count(context, now)
        volume_1h = self._volume_1h(context, now)

        amount_text = format_minor(context.amount_minor, context.currency)
        is_new_payee = payee_age_minutes is not None and (
            timedelta(minutes=payee_age_minutes) < NEW_PAYEE_WINDOW
        )
        if is_new_payee and context.amount_minor >= NEW_PAYEE_LARGE_MINOR:
            score += 45
            reasons.append(
                FraudReason(
                    code="NEW_PAYEE_LARGE_AMOUNT",
                    message=(
                        f"Payee was added {payee_age_minutes} minutes ago "
                        f"and {amount_text} is a large amount"
                    ),
                    source=ReasonSource.RULE,
                    contribution=45,
                )
            )
        elif is_new_payee:
            score += 15
            reasons.append(
                FraudReason(
                    code="NEW_PAYEE",
                    message=f"Payee was added {payee_age_minutes} minutes ago",
                    source=ReasonSource.RULE,
                    contribution=15,
                )
            )
        if history_count >= SPIKE_MIN_HISTORY and average > 0:
            ratio = context.amount_minor / average
            if ratio >= SPIKE_MULTIPLIER:
                score += 35
                reasons.append(
                    FraudReason(
                        code="AMOUNT_SPIKE",
                        message=f"Amount is {ratio:.1f}x this account's usual transfer",
                        source=ReasonSource.RULE,
                        contribution=35,
                    )
                )
        if recent_count + 1 >= VELOCITY_COUNT:
            score += 30
            reasons.append(
                FraudReason(
                    code="VELOCITY_BURST",
                    message=f"{recent_count + 1} transfers from this account within 10 minutes",
                    source=ReasonSource.RULE,
                    contribution=30,
                )
            )
        if context.amount_minor >= LARGE_AMOUNT_MINOR:
            score += 20
            reasons.append(
                FraudReason(
                    code="LARGE_AMOUNT",
                    message=f"{amount_text} exceeds the large-transfer threshold",
                    source=ReasonSource.RULE,
                    contribution=20,
                )
            )

        new_country = self._new_country(context, now)
        if new_country is not None:
            score += NEW_COUNTRY_POINTS
            reasons.append(new_country)

        failures = self._login_failures(context, now)
        new_device, new_network = self._new_device_or_network(context, now)
        for reason in _session_signals(failures, new_device, new_network, context.ip_network):
            score += int(reason.contribution or 0)
            reasons.append(reason)

        score = min(score, 100)
        return FraudScoreResult(
            risk_score=score,
            decision=decide(score),
            reasons=reasons,
            model_version=MODEL_VERSION,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            features={
                "amount_minor": context.amount_minor,
                "payee_age_minutes": payee_age_minutes,
                "avg_outgoing_90d_minor": round(average, 2),
                "outgoing_count_90d": int(history_count),
                "transfers_last_10m": int(recent_count),
                "volume_1h_minor": volume_1h,
                "ip_country": context.ip_country,
                "ip_network": context.ip_network,
                "device_id": str(context.device_id) if context.device_id else None,
                "login_failures_24h": failures,
                "new_device": new_device,
                "new_network": new_network,
            },
        )

    def _payee_age_minutes(self, context: TransactionContext, now: datetime) -> int | None:
        if context.payee_id is None:
            return None
        created = self.session.execute(
            select(Payee.created_at).where(Payee.id == context.payee_id)
        ).scalar_one_or_none()
        return None if created is None else int((now - created).total_seconds() // 60)

    def _recent_count(self, context: TransactionContext, now: datetime) -> int:
        """Transfers from this account in 10 minutes: Redis first, PostgreSQL if Redis is down."""
        if self.counters is not None:
            cached = self.counters.transfers_in_window(
                context.account_id, now, exclude=context.transaction_id
            )
            if cached is not None:
                return cached
        return int(
            self.session.execute(
                select(func.count()).where(
                    Transaction.debit_account_id == context.account_id,
                    Transaction.type == TransactionType.TRANSFER,
                    Transaction.id != context.transaction_id,
                    Transaction.created_at >= now - VELOCITY_WINDOW,
                )
            ).scalar_one()
        )

    def _volume_1h(self, context: TransactionContext, now: datetime) -> int:
        if self.counters is not None:
            cached = self.counters.volume_in_window(
                context.account_id, now, exclude=context.transaction_id
            )
            if cached is not None:
                return cached
        total = self.session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.debit_account_id == context.account_id,
                Transaction.type == TransactionType.TRANSFER,
                Transaction.id != context.transaction_id,
                Transaction.created_at >= now - VOLUME_WINDOW,
            )
        ).scalar_one()
        return int(total)

    def _login_failures(self, context: TransactionContext, now: datetime) -> int:
        """Failed sign-ins on the customer's login in 24 hours (``security_events``)."""
        user_id = (
            select(Customer.user_id).where(Customer.id == context.customer_id).scalar_subquery()
        )
        count = self.session.execute(
            select(func.count()).where(
                SecurityEvent.user_id == user_id,
                SecurityEvent.event_type == SecurityEventType.LOGIN_FAILURE,
                SecurityEvent.created_at >= now - LOGIN_FAILURE_WINDOW,
            )
        ).scalar_one()
        return int(count)

    def _new_device_or_network(
        self, context: TransactionContext, now: datetime
    ) -> tuple[bool, bool]:
        """Device / network never seen on this customer's scored transfers (180 days).

        Like NEW_COUNTRY, a customer with no history yet, or a transfer with no device/network,
        never adds risk: there is nothing to differ from. Registered devices count as seen.
        """
        if context.device_id is None and context.ip_network is None:
            return False, False
        rows = self.session.execute(
            select(
                FraudEvent.features["device_id"].astext, FraudEvent.features["ip_network"].astext
            ).where(
                FraudEvent.customer_id == context.customer_id,
                FraudEvent.created_at >= now - COUNTRY_HISTORY_WINDOW,
            )
        ).all()
        devices = {d for d, _ in rows if d}
        networks = {n for _, n in rows if n}
        registered = self.session.execute(
            select(Device.fingerprint_hash).where(Device.customer_id == context.customer_id)
        ).scalars()
        devices |= {str(device_uuid_from_hash(h)) for h in registered}
        device = str(context.device_id) if context.device_id else None
        new_device = device is not None and bool(devices) and device not in devices
        new_network = (
            context.ip_network is not None and bool(networks) and context.ip_network not in networks
        )
        return new_device, new_network

    def _new_country(self, context: TransactionContext, now: datetime) -> FraudReason | None:
        """The transfer's IP country was not seen in this customer's scored transfers recently.

        Unknown country (no database, private IP, lookup miss) never adds risk, and neither does a
        customer's first located transfer: there is no baseline to differ from.
        """
        if context.ip_country is None:
            return None
        country = FraudEvent.features["ip_country"].astext
        seen = set(
            self.session.execute(
                select(country)
                .where(
                    FraudEvent.customer_id == context.customer_id,
                    FraudEvent.created_at >= now - COUNTRY_HISTORY_WINDOW,
                    country.is_not(None),
                )
                .distinct()
            ).scalars()
        )
        if not seen or context.ip_country in seen:
            return None
        listed = sorted(seen)
        shown = ", ".join(listed[:5]) + (f" and {len(listed) - 5} more" if len(listed) > 5 else "")
        return FraudReason(
            code="NEW_COUNTRY",
            message=(
                f"IP located in {context.ip_country}; recent transfers came from {shown} "
                "(approximate, ADR-0014)"
            ),
            source=ReasonSource.RULE,
            contribution=NEW_COUNTRY_POINTS,
        )


def _session_signals(
    failures: int, new_device: bool, new_network: bool, network: str | None
) -> list[FraudReason]:
    """RECENT_LOGIN_FAILURES and NEW_DEVICE_OR_NETWORK (TASK-095): the UC-09 triggers "multiple
    login failures" and "new location" read as a new device or network."""
    reasons = []
    if failures >= LOGIN_FAILURES_MIN:
        reasons.append(
            FraudReason(
                code="RECENT_LOGIN_FAILURES",
                message=f"{failures} failed sign-ins on this login in the last 24 hours",
                source=ReasonSource.RULE,
                contribution=LOGIN_FAILURES_POINTS,
            )
        )
    if new_device or new_network:
        parts = [
            p for p, hit in (("a device", new_device), (f"network {network}", new_network)) if hit
        ]
        reasons.append(
            FraudReason(
                code="NEW_DEVICE_OR_NETWORK",
                message=(
                    f"Sent from {' and '.join(parts)} not seen on this customer's recent transfers"
                ),
                source=ReasonSource.RULE,
                contribution=NEW_DEVICE_OR_NETWORK_POINTS,
            )
        )
    return reasons
