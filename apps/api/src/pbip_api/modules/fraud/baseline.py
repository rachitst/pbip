"""Baseline, fully explainable rules scorer (deterministic stop-gap until the ML engine ships).

Implements the ``FraudScorer`` contract. Every point of score is attributed to a named rule with
a human-readable message, so an analyst can always see WHY a transfer was flagged. The downstream
hybrid engine (rules + LightGBM + Isolation Forest + SHAP) wraps this behind the same
interface; these rules become its deterministic layer.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from pbip_ml.fraud.contract import (
    FraudDecision,
    FraudReason,
    FraudScoreResult,
    ReasonSource,
    TransactionContext,
)
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.selectable import ScalarSelect

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import utcnow
from pbip_api.core.money import format_minor
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.accounts.models import Account, AccountHolder
from pbip_api.modules.auth.models import SecurityEvent, SecurityEventType
from pbip_api.modules.customers.models import Customer, Device
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.fraud.signals import device_uuid_from_hash
from pbip_api.modules.fraud.travel import TravelPolicy, impossible_travel
from pbip_api.modules.ledger.models import Transaction, TransactionStatus, TransactionType
from pbip_api.modules.payees.models import Payee

MODEL_VERSION = "baseline-rules-0.6"  # 0.2: NEW_COUNTRY (ADR-0014); 0.3: TASK-095 signals;
# 0.4: IMPOSSIBLE_TRAVEL (ADR-0034); 0.5: MULE_PASS_THROUGH (#144, ADR-0035);
# 0.6: STRUCTURING (#274) and DORMANT_REACTIVATION (#275), ADR-0046

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
# MULE_PASS_THROUGH (ADR-0035): alone STEP_UP, with NEW_PAYEE (+15) REVIEW; a repeat alone REVIEW.
MULE_POINTS = 45
MULE_REPEAT_POINTS = 60
MULE_REPEAT_WINDOW = timedelta(days=30)
MULE_SNAPSHOT_IDS = 20  # inbound ids kept in the snapshot; the count and total are always complete
# STRUCTURING and DORMANT_REACTIVATION (ADR-0046): each alone STEP_UP; with NEW_PAYEE_LARGE_AMOUNT
# (+45) REVIEW (80), not BLOCK.
STRUCTURING_POINTS = 35
STRUCTURING_SNAPSHOT_IDS = 20
DORMANT_POINTS = 35
# Customer-induced activity wakes an account; bank-initiated postings (fees, deposit interest,
# maturity payouts, reversals, loan disbursals and collections) do not (ADR-0046).
ACTIVITY_TYPES = (
    TransactionType.TRANSFER,
    TransactionType.DEPOSIT,
    TransactionType.BILL_PAYMENT,
    TransactionType.FD_BOOKING,
)

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

    def __init__(
        self,
        session: Session,
        counters: VelocityCounters | None = None,
        travel: TravelPolicy | None = None,
        settings: Settings | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session = session
        self.counters = counters  # Redis windows (TASK-034); None or unreachable -> PostgreSQL
        self.travel = travel or TravelPolicy.disabled()  # no city database: rule never scores
        self.settings = settings or get_settings()
        self.clock = clock  # injected by tests; scoring reads no other time source

    def score(self, context: TransactionContext) -> FraudScoreResult:
        started = time.perf_counter()
        now = self.clock()
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
        travel_reason, travel_features = impossible_travel(self.session, context, self.travel)
        mule = self._mule_pass_through(context, now)
        structuring = self._structuring(context, now)
        dormant = self._dormant_reactivation(context, now)
        late_rules = (travel_reason, mule.reason, structuring.reason, dormant.reason)
        for reason in [
            *_session_signals(failures, new_device, new_network, context.ip_network),
            *(rule for rule in late_rules if rule is not None),
        ]:
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
                "geo_lat": context.geo_lat,
                "geo_lon": context.geo_lon,
                **travel_features,
                **mule.features,
                **structuring.features,
                **dormant.features,
            },
        )

    def _mule_pass_through(self, context: TransactionContext, now: datetime) -> "_RuleCheck":
        """F6: money received from other customers in the window, most of it now sent on.

        Inbound is POSTED TRANSFER credits from accounts held by other customers only: DEPOSIT
        (the funding path), REVERSAL and moves between this customer's own accounts are not
        someone else's money passing through. Read-only: no lock, no write.
        """
        cfg = self.settings
        window = timedelta(minutes=cfg.fraud_mule_window_minutes)
        own = select(AccountHolder.account_id).where(
            AccountHolder.customer_id == context.customer_id
        )
        others = select(AccountHolder.account_id).where(
            AccountHolder.customer_id != context.customer_id
        )
        inbound = (
            Transaction.credit_account_id == context.account_id,  # ix_..._credit_account_id_..
            Transaction.created_at >= now - window,
            Transaction.type == TransactionType.TRANSFER,
            Transaction.status == TransactionStatus.POSTED,
            Transaction.debit_account_id.in_(others),
            Transaction.debit_account_id.not_in(own),
        )
        summed, count, first = self.session.execute(
            select(
                func.coalesce(func.sum(Transaction.amount), 0),
                func.count(),
                func.min(Transaction.created_at),
            ).where(*inbound)
        ).one()
        total = int(summed)
        ids = (
            self.session.execute(
                select(Transaction.id)
                .where(*inbound)
                .order_by(Transaction.created_at)
                .limit(MULE_SNAPSHOT_IDS)
            ).scalars()
            if count
            else ()
        )
        check = _RuleCheck(
            features={
                "mule_window_minutes": cfg.fraud_mule_window_minutes,
                "mule_inbound_total_minor": total,
                "mule_inbound_count": int(count),
                "mule_ratio": round(context.amount_minor / total, 4) if total else None,
                # staff-only (fraud queue); the snapshot holds scalars, so ids are comma-joined
                "mule_inbound_transaction_ids": ",".join(str(i) for i in ids),
            }
        )
        fires = (
            total >= cfg.fraud_mule_min_inbound_minor
            and context.amount_minor * 100 >= cfg.fraud_mule_min_ratio_percent * total
        )
        check.features["mule_pass_through"] = fires
        if not fires:
            return check
        earlier = self._mule_episodes(context, now)
        check.features["mule_earlier_episodes_30d"] = earlier
        repeat = earlier >= cfg.fraud_mule_repeat_episodes
        minutes = int((now - first).total_seconds() // 60)
        message = (
            f"Received {format_minor(total, context.currency)} in {count} "
            f"transfer{'s' if count != 1 else ''} from other customers, the first "
            f"{minutes} min ago; sending {format_minor(context.amount_minor, context.currency)} "
            f"({context.amount_minor * 100 // total}%)"
        )
        if repeat:
            message += f"; repeat pattern: {earlier} earlier pass-throughs in 30 days"
        points = MULE_REPEAT_POINTS if repeat else MULE_POINTS
        check.reason = FraudReason(
            code="MULE_PASS_THROUGH",
            message=message,
            source=ReasonSource.RULE,
            contribution=points,
        )
        return check

    def _structuring(self, context: TransactionContext, now: datetime) -> "_RuleCheck":
        """F7: several movements, each just under the threshold, on this account in the window.

        Counted: POSTED TRANSFER or DEPOSIT credits from accounts this customer does not hold,
        POSTED TRANSFER debits to accounts this customer does not hold, and this transfer if it is
        just under the threshold too. Moves between the customer's own accounts are not counted.
        Read-only: no lock, no write (ix_transactions_{debit,credit}_account_id_created_at).
        """
        cfg = self.settings
        threshold = cfg.fraud_structuring_threshold_minor
        floor_x100 = threshold * (100 - cfg.fraud_structuring_band_percent)  # exact, no float
        own = select(AccountHolder.account_id).where(
            AccountHolder.customer_id == context.customer_id
        )
        received = and_(
            Transaction.credit_account_id == context.account_id,
            Transaction.type.in_((TransactionType.TRANSFER, TransactionType.DEPOSIT)),
            or_(Transaction.debit_account_id.is_(None), Transaction.debit_account_id.not_in(own)),
        )
        sent = and_(
            Transaction.debit_account_id == context.account_id,
            Transaction.type == TransactionType.TRANSFER,
            or_(Transaction.credit_account_id.is_(None), Transaction.credit_account_id.not_in(own)),
        )
        near = (
            Transaction.status == TransactionStatus.POSTED,
            Transaction.id != context.transaction_id,
            Transaction.created_at >= now - timedelta(hours=cfg.fraud_structuring_window_hours),
            Transaction.amount * 100 >= floor_x100,
            Transaction.amount < threshold,
            or_(received, sent),
        )
        received_count, sent_count, summed = self.session.execute(
            select(
                func.count().filter(Transaction.credit_account_id == context.account_id),
                func.count().filter(Transaction.debit_account_id == context.account_id),
                func.coalesce(func.sum(Transaction.amount), 0),
            ).where(*near)
        ).one()
        this_near = context.amount_minor * 100 >= floor_x100 and context.amount_minor < threshold
        count = int(received_count) + int(sent_count) + int(this_near)
        total = int(summed) + (context.amount_minor if this_near else 0)
        ids = (
            self.session.execute(
                select(Transaction.id)
                .where(*near)
                .order_by(Transaction.created_at)
                .limit(STRUCTURING_SNAPSHOT_IDS)
            ).scalars()
            if count > int(this_near)
            else ()
        )
        fires = count >= cfg.fraud_structuring_min_count
        check = _RuleCheck(
            features={
                "structuring_window_hours": cfg.fraud_structuring_window_hours,
                "structuring_threshold_minor": threshold,
                "structuring_band_percent": cfg.fraud_structuring_band_percent,
                "structuring_received_count": int(received_count),
                "structuring_sent_count": int(sent_count),
                "structuring_this_transfer": this_near,
                "structuring_total_minor": total,
                # staff-only (fraud queue); the snapshot holds scalars, so ids are comma-joined
                "structuring_transaction_ids": ",".join(str(i) for i in ids),
                "structuring": fires,
            }
        )
        if fires:
            check.reason = FraudReason(
                code="STRUCTURING",
                message=(
                    f"{count} movements just under {format_minor(threshold, context.currency)} "
                    f"(at least {100 - cfg.fraud_structuring_band_percent}% of it) in "
                    f"{cfg.fraud_structuring_window_hours} h: {int(received_count)} received, "
                    f"{int(sent_count) + int(this_near)} sent"
                    f"{' including this transfer' if this_near else ''}; together "
                    f"{format_minor(total, context.currency)}"
                ),
                source=ReasonSource.RULE,
                contribution=STRUCTURING_POINTS,
            )
        return check

    def _dormant_reactivation(self, context: TransactionContext, now: datetime) -> "_RuleCheck":
        """F8: a large transfer from an account that was dormant until it woke up recently.

        The account woke at its first customer-induced activity in the reactivation window (or
        now, with this transfer). The gap is from the last activity before the window (or the
        account's opening, if there was none) to that wake-up, so a small first transfer does
        not hide a large second one. Read-only: four index range scans on
        ix_transactions_{debit,credit}_account_id_created_at, each stopping at its first row.

        One clock (#442): ``now`` and every ``created_at`` read here (transactions, the account's
        opening) come from the app clock; the ledger and account opening stamp them with it. The gap
        cannot be negative (the last activity is before the window, the wake-up in it or now); the
        days since waking are floored at 0, so a clock stepped back never shows "-1 days ago".
        """
        cfg = self.settings
        since = now - timedelta(days=cfg.fraud_dormant_reactivation_days)
        activity = (
            Transaction.status == TransactionStatus.POSTED,
            Transaction.type.in_(ACTIVITY_TYPES),
            Transaction.id != context.transaction_id,
        )

        def edge(
            column: InstrumentedAttribute[UUID | None], newest: bool
        ) -> ScalarSelect[datetime]:
            aggregate = func.max if newest else func.min
            window = Transaction.created_at < since if newest else Transaction.created_at >= since
            return (
                select(aggregate(Transaction.created_at))
                .where(column == context.account_id, window, *activity)
                .scalar_subquery()
            )

        sides = (Transaction.debit_account_id, Transaction.credit_account_id)
        woke_at, last_at, opened_at = self.session.execute(
            select(
                func.least(*(edge(side, newest=False) for side in sides)),
                func.greatest(*(edge(side, newest=True) for side in sides)),
                select(Account.created_at).where(Account.id == context.account_id)
                .scalar_subquery(),
            )
        ).one()  # fmt: skip
        if last_at is None and opened_at is not None and opened_at < since:
            last_at = opened_at  # never used before the window: dormant since it was opened
        woke = woke_at if woke_at is not None else now
        gap_days = None if last_at is None else int((woke - last_at).total_seconds() // 86_400)
        fires = (
            gap_days is not None
            and gap_days >= cfg.fraud_dormancy_days
            and context.amount_minor >= cfg.fraud_dormant_min_amount_minor
        )
        check = _RuleCheck(
            features={
                "dormancy_days": cfg.fraud_dormancy_days,
                "dormant_reactivation_days": cfg.fraud_dormant_reactivation_days,
                "dormant_gap_days": gap_days,
                "dormant_last_activity_at": last_at.isoformat() if last_at else None,
                "dormant_woke_at": woke_at.isoformat() if woke_at else None,
                "dormant_reactivation": fires,
            }
        )
        if fires:
            woke_text = "until this transfer"
            if woke_at is not None:
                days_ago = max(0, int((now - woke_at).total_seconds() // 86_400))
                woke_text = f"until it became active again {days_ago} days ago"
            check.reason = FraudReason(
                code="DORMANT_REACTIVATION",
                message=(
                    f"No customer activity on this account for {gap_days} days {woke_text}; "
                    f"sending {format_minor(context.amount_minor, context.currency)}"
                ),
                source=ReasonSource.RULE,
                contribution=DORMANT_POINTS,
            )
        return check

    def _mule_episodes(self, context: TransactionContext, now: datetime) -> int:
        """Earlier transfers from this account flagged MULE_PASS_THROUGH in 30 days."""
        count = self.session.execute(
            select(func.count(func.distinct(Transaction.id)))
            .join(FraudEvent, FraudEvent.transaction_id == Transaction.id)
            .where(
                Transaction.debit_account_id == context.account_id,
                Transaction.created_at >= now - MULE_REPEAT_WINDOW,
                Transaction.id != context.transaction_id,
                FraudEvent.reasons.contains([{"code": "MULE_PASS_THROUGH"}]),
            )
        ).scalar_one()
        return int(count)

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


@dataclass
class _RuleCheck:
    features: dict[str, float | int | str | bool | None]
    reason: FraudReason | None = field(default=None)


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
