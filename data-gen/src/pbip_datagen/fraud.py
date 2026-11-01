"""Synthetic dataset v2 (TASK-023, TASK-071): P2P transfers with context, labelled fraud F1-F8.

v1 (``generator.py``) produces income, bills and card-style spending. Fraud happens on transfers, so
v2 adds, for every customer, ordinary transfers to established payees and, for a configured share of
customers, ONE injected incident of a scenario:

* F1 APP scam: a payee added minutes before a transfer of 1.5-4x the customer's monthly income.
* F2 velocity burst: 6-12 transfers inside 30 minutes to 2-4 payees.
* F3 account takeover: 3-6 failed logins, then a login from a new device and a draining transfer
  to a new payee within the hour.
* F4 impossible travel: a transfer from the home city, then one from abroad 1-3 hours later.
* F5 amount anomaly: one transfer of 6-15x the customer's median ordinary transfer, to a known
  payee.
* F6 mule pass-through: 3-6 credits from unrelated senders, then 90-98% of the inflow sent out to a
  new payee within 6 hours.
* F7 structuring (v2.2, TASK-071): 3-6 deposits from different senders within 3 days, each just
  beneath the reporting threshold (90-99.5% of it, in 0.5% steps such as INR 99,500), together
  well above it. The synthetic threshold is INR 1,00,000, the platform's LARGE_AMOUNT rule.
* F8 dormant-account reactivation (v2.2, TASK-071): an account whose customer went inactive
  (``churned_on``: no income, spending or transfers after it) moves 1-2 transfers of the maximum
  per-transfer amount (INR 2,00,000, the API's ``max_transfer_minor``) to a payee added minutes
  earlier, from a device never seen, more than ``dormancy_days`` (180) after the last activity.
  Only dormant accounts qualify, so its rate is the share of DORMANT accounts that are hit, and it
  needs a dataset long enough to contain the dormancy (none in 6 months; some in 12). v2.0 still
  makes ordinary transfers after ``churned_on``; for an F8 customer those are dropped, as a
  dormant account has none (the only v2.1 rows TASK-071 removes).

Every row of an incident is labelled (``fraud_labels``: transaction id -> scenario, incident, role).
Normal customers also do things for the first time (v2.1, #251), because real ones do:
* L1 a new payee, paid minutes to days after it was added (a new landlord, a one-off purchase,
  splitting a bill), at ordinary amounts or occasionally a one-off of up to 60% of monthly income;
  the payee is then used again like any other.
* L2 a new phone: from some date on, the customer's transfers come from a new device.
* L3 a forgotten password: 1-4 failed logins, then a normal transfer from the usual device.
None of these reaches a scenario's full signature (a new payee never gets 1.5x income or more, a
new device never comes with failed logins or a new payee), so labels stay clean by construction.
They draw from their own random stream, so ordinary and fraud rows are identical to v2.0.
F7 and F8 (v2.2) are drawn last, on a third stream ``[seed, index, 9]``, only for customers with no
F1-F6 incident, so every v2.1 row and label is unchanged (``test_v2_1_rows_are_unchanged``) except
the post-churn ordinary transfers of an F8 customer (above).

Determinism: each customer draws from ``default_rng([seed, index, 7])`` (independent of v1's stream
``[seed, index, 1]``) and ids are UUIDv5 from the run, so the same config yields identical output.
Money is integer paise; no wall clock is read.
"""

import itertools
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

import numpy as np

from pbip_datagen.calendars import at_ist, month_starts
from pbip_datagen.generator import (
    CITIES,
    GeneratorConfig,
    SyntheticCustomer,
    iter_customers,
    run_id_for,
)

FRAUD_VERSION = "2.2.0"  # 2.1: legitimate first-time behaviour (#251); 2.2: F7, F8 (TASK-071)
Scenario = Literal["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"]
SCENARIOS: tuple[Scenario, ...] = ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8")
_V2_0_SCENARIOS = SCENARIOS[:6]  # drawn on the original stream, exactly as in v2.0 and v2.1
SCENARIO_NAMES: dict[Scenario, str] = {
    "F1": "APP scam: new payee and large transfer",
    "F2": "Velocity burst",
    "F3": "Account takeover sequence",
    "F4": "Impossible travel / geo jump",
    "F5": "Amount anomaly vs customer baseline",
    "F6": "Mule pass-through flow",
    "F7": "Structuring: deposits just below the reporting threshold",
    "F8": "Dormant account suddenly moving the maximum",
}
# Share of customers who suffer one incident of each scenario (a customer gets at most one).
# F8 is the share of DORMANT accounts (inactive for longer than ``dormancy_days``) that are hit.
DEFAULT_RATES: dict[Scenario, float] = {
    "F1": 0.02,
    "F2": 0.01,
    "F3": 0.01,
    "F4": 0.01,
    "F5": 0.015,
    "F6": 0.005,
    "F7": 0.01,
    "F8": 0.2,
}
# Synthetic, not statutory: the platform's LARGE_AMOUNT rule (pbip_api.modules.fraud.baseline).
DEFAULT_REPORTING_THRESHOLD_MINOR = 1_00_000_00  # INR 1,00,000
# The API's per-transfer cap (pbip_api.core.config: max_transfer_minor); two fit the daily limit.
DEFAULT_MAX_TRANSFER_MINOR = 2_00_000_00  # INR 2,00,000
DEFAULT_DORMANCY_DAYS = 180
FOREIGN_COUNTRIES = ("AE", "SG", "GB", "US", "NG", "RU")
_PAYEE_MIN_DAYS = 30  # ordinary payees are at least this old
_MIN_MINOR = 100

Direction = Literal["DEBIT", "CREDIT"]


@dataclass(frozen=True, slots=True)
class FraudConfig:
    rates: dict[Scenario, float] = field(default_factory=lambda: dict(DEFAULT_RATES))
    reporting_threshold_minor: int = DEFAULT_REPORTING_THRESHOLD_MINOR
    max_transfer_minor: int = DEFAULT_MAX_TRANSFER_MINOR
    dormancy_days: int = DEFAULT_DORMANCY_DAYS

    def __post_init__(self) -> None:
        if set(self.rates) != set(SCENARIOS):
            raise ValueError("rates must cover F1-F8 exactly")
        if any(not 0 <= r <= 0.2 for r in self.rates.values()) or sum(self.rates.values()) > 0.5:
            raise ValueError("each rate must be within 0-0.2 and all together at most 0.5")
        if self.reporting_threshold_minor < 200 * _MIN_MINOR:  # 0.5% steps of at least INR 1
            raise ValueError("reporting_threshold_minor must be at least 20000")
        if self.max_transfer_minor < _MIN_MINOR:
            raise ValueError("max_transfer_minor must be at least 100")
        if not 180 <= self.dormancy_days <= 3650:
            raise ValueError("dormancy_days must be within 180-3650")


@dataclass(frozen=True, slots=True)
class Transfer:
    """A P2P movement with the context a fraud model sees at decision time."""

    id: uuid.UUID
    customer_id: uuid.UUID
    occurred_at: datetime
    direction: Direction
    amount_minor: int
    counterparty_id: uuid.UUID  # the payee (DEBIT) or the sender (CREDIT)
    payee_added_at: datetime | None  # DEBIT only
    device_id: uuid.UUID
    new_device: bool
    ip_country: str
    ip_city: str
    failed_logins_24h: int


@dataclass(frozen=True, slots=True)
class FraudLabel:
    transaction_id: uuid.UUID
    scenario: Scenario
    incident_id: uuid.UUID
    role: str  # e.g. "transfer", "burst", "takeover", "home", "abroad", "inbound", "outbound",
    # "deposit" (F7), "reactivation" (F8)
    fraudulent: bool  # False only for context legs (F4's genuine transfer from home)


@dataclass(frozen=True, slots=True)
class LabelledTransfers:
    run_id: uuid.UUID
    transfers: tuple[Transfer, ...]
    labels: tuple[FraudLabel, ...]

    def summary(self) -> dict[str, object]:
        """Counts per scenario: incidents (customers affected) and labelled transactions."""
        incidents = {
            s: len({lb.incident_id for lb in self.labels if lb.scenario == s}) for s in SCENARIOS
        }
        txns = Counter(lb.scenario for lb in self.labels if lb.fraudulent)
        return {
            "version": FRAUD_VERSION,
            "run_id": str(self.run_id),
            "transfers": len(self.transfers),
            "fraud_transactions": sum(lb.fraudulent for lb in self.labels),
            "context_transactions": sum(not lb.fraudulent for lb in self.labels),
            "scenarios": {
                s: {"name": SCENARIO_NAMES[s], "incidents": incidents[s], "transactions": txns[s]}
                for s in SCENARIOS
            },
        }


class _Ids:
    """Deterministic ids per customer: a UUIDv5 base and a counter in the low 24 bits."""

    def __init__(self, customer: SyntheticCustomer, kind: str) -> None:
        self._base = uuid.uuid5(customer.id, kind).int & ~0xFFFFFF
        self._counter = itertools.count()

    def next(self) -> uuid.UUID:
        return uuid.UUID(int=self._base | next(self._counter))


@dataclass
class _Ctx:
    customer: SyntheticCustomer
    rng: np.random.Generator
    window_start: datetime
    window_end: datetime
    home_city: str
    device: uuid.UUID
    payees: list[tuple[uuid.UUID, datetime]]
    tx_ids: _Ids
    party_ids: _Ids
    rows: list[Transfer] = field(default_factory=list)

    def money(self, value: float) -> int:
        return max(round(value / 100.0) * 100, _MIN_MINOR)

    def add(self, when: datetime, direction: Direction, amount: int, counterparty: uuid.UUID,
            payee_added_at: datetime | None, *, device: uuid.UUID | None = None,
            country: str = "IN", city: str | None = None, failed: int = 0) -> Transfer:  # fmt: skip
        row = Transfer(
            id=self.tx_ids.next(),
            customer_id=self.customer.id,
            occurred_at=when,
            direction=direction,
            amount_minor=amount,
            counterparty_id=counterparty,
            payee_added_at=payee_added_at,
            device_id=device or self.device,
            new_device=device is not None and device != self.device,
            ip_country=country,
            ip_city=city or self.home_city,
            failed_logins_24h=failed,
        )
        self.rows.append(row)
        return row

    def moment(self, lead: timedelta = timedelta(0)) -> datetime:
        """A random daytime instant in the window, leaving ``lead`` before the end."""
        span = (self.window_end - lead - self.window_start).total_seconds()
        offset = timedelta(seconds=float(self.rng.uniform(0, max(span, 1))))
        base = self.window_start + offset
        return base.replace(
            hour=int(self.rng.integers(9, 21)), minute=int(self.rng.integers(0, 60))
        )


def _ordinary_transfers(ctx: _Ctx, months: list[datetime]) -> None:
    income = ctx.customer.monthly_income_minor
    for month in months:
        for _ in range(int(ctx.rng.integers(0, 5))):
            payee, added = ctx.payees[int(ctx.rng.integers(0, len(ctx.payees)))]
            day = int(ctx.rng.integers(1, 28))
            when = at_ist(month.date().replace(day=day), int(ctx.rng.integers(8 * 60, 22 * 60)))
            amount = ctx.money(income * float(ctx.rng.uniform(0.01, 0.12)))
            ctx.add(when, "DEBIT", amount, payee, added)


def _median_debit(ctx: _Ctx) -> int:
    debits = sorted(t.amount_minor for t in ctx.rows if t.direction == "DEBIT")
    return debits[len(debits) // 2] if debits else ctx.customer.monthly_income_minor // 20


def _f1(ctx: _Ctx) -> list[tuple[Transfer, str]]:
    when = ctx.moment()
    added = when - timedelta(minutes=int(ctx.rng.integers(3, 50)))
    amount = ctx.money(ctx.customer.monthly_income_minor * float(ctx.rng.uniform(1.5, 4.0)))
    return [(ctx.add(when, "DEBIT", amount, ctx.party_ids.next(), added), "transfer")]


def _f2(ctx: _Ctx) -> list[tuple[Transfer, str]]:
    start = ctx.moment(timedelta(hours=1))
    targets = [ctx.party_ids.next() for _ in range(int(ctx.rng.integers(2, 5)))]
    offsets = sorted(float(x) for x in ctx.rng.uniform(0, 29 * 60, int(ctx.rng.integers(6, 13))))
    rows = []
    for seconds in offsets:
        when = start + timedelta(seconds=seconds)
        payee = targets[int(ctx.rng.integers(0, len(targets)))]
        amount = ctx.money(ctx.customer.monthly_income_minor * float(ctx.rng.uniform(0.05, 0.2)))
        rows.append((ctx.add(when, "DEBIT", amount, payee, when - timedelta(minutes=90)), "burst"))
    return rows


def _f3(ctx: _Ctx) -> list[tuple[Transfer, str]]:
    when = ctx.moment(timedelta(hours=2))
    device = uuid.uuid5(ctx.customer.id, f"attacker-device-{when.isoformat()}")
    failed = int(ctx.rng.integers(3, 7))
    amount = ctx.money(ctx.customer.monthly_income_minor * float(ctx.rng.uniform(0.8, 2.5)))
    payee_added = when - timedelta(minutes=int(ctx.rng.integers(5, 40)))
    row = ctx.add(when, "DEBIT", amount, ctx.party_ids.next(), payee_added, device=device,
                  failed=failed)  # fmt: skip
    return [(row, "takeover")]


def _f4(ctx: _Ctx) -> list[tuple[Transfer, str]]:
    first = ctx.moment(timedelta(hours=4))
    payee, added = ctx.payees[0]
    home = ctx.add(
        first, "DEBIT", ctx.money(ctx.customer.monthly_income_minor * 0.05), payee, added
    )
    later = first + timedelta(minutes=int(ctx.rng.integers(60, 180)))
    country = FOREIGN_COUNTRIES[int(ctx.rng.integers(0, len(FOREIGN_COUNTRIES)))]
    amount = ctx.money(ctx.customer.monthly_income_minor * float(ctx.rng.uniform(0.3, 1.2)))
    abroad = ctx.add(later, "DEBIT", amount, payee, added, country=country, city="(abroad)")
    return [(home, "home"), (abroad, "abroad")]


def _f5(ctx: _Ctx) -> list[tuple[Transfer, str]]:
    when = ctx.moment()
    payee, added = ctx.payees[int(ctx.rng.integers(0, len(ctx.payees)))]
    amount = ctx.money(_median_debit(ctx) * float(ctx.rng.uniform(6, 15)))
    return [(ctx.add(when, "DEBIT", amount, payee, added), "transfer")]


def _f6(ctx: _Ctx) -> list[tuple[Transfer, str]]:
    start = ctx.moment(timedelta(hours=8))
    rows, inflow = [], 0
    for i in range(int(ctx.rng.integers(3, 7))):
        amount = ctx.money(ctx.customer.monthly_income_minor * float(ctx.rng.uniform(0.2, 0.8)))
        inflow += amount
        when = start + timedelta(minutes=20 * i + int(ctx.rng.integers(0, 15)))
        rows.append((ctx.add(when, "CREDIT", amount, ctx.party_ids.next(), None), "inbound"))
    out_at = rows[-1][0].occurred_at + timedelta(minutes=int(ctx.rng.integers(15, 300)))
    out_amount = ctx.money(inflow * float(ctx.rng.uniform(0.90, 0.98)))
    payee_added = out_at - timedelta(minutes=10)
    rows.append(
        (ctx.add(out_at, "DEBIT", out_amount, ctx.party_ids.next(), payee_added), "outbound")
    )
    return rows


def _f7(ctx: _Ctx, threshold: int) -> list[tuple[Transfer, str]]:
    """Structuring: deposits from different senders, each 0.5-10% under the threshold."""
    start = ctx.moment(timedelta(days=3))
    step = threshold // 200  # 0.5% of the threshold, e.g. INR 500
    rows = []
    for _ in range(int(ctx.rng.integers(3, 7))):
        day = start + timedelta(days=int(ctx.rng.integers(0, 3)))
        when = day.replace(hour=int(ctx.rng.integers(9, 21)), minute=int(ctx.rng.integers(0, 60)))
        amount = threshold - step * int(ctx.rng.integers(1, 21))
        rows.append((ctx.add(when, "CREDIT", amount, ctx.party_ids.next(), None), "deposit"))
    return rows


def _f8(ctx: _Ctx, first: date, last: date, amount: int) -> list[tuple[Transfer, str]]:
    """Dormant account: 1-2 maximum transfers to a payee added minutes earlier, new device."""
    day = first + timedelta(days=int(ctx.rng.integers(0, (last - first).days + 1)))
    when = at_ist(day, int(ctx.rng.integers(9 * 60, 21 * 60)))
    device = uuid.uuid5(ctx.customer.id, f"dormant-device-{when.isoformat()}")
    payee, added = ctx.party_ids.next(), when - timedelta(minutes=int(ctx.rng.integers(5, 45)))
    rows = []
    for _ in range(int(ctx.rng.integers(1, 3))):
        rows.append((ctx.add(when, "DEBIT", amount, payee, added, device=device), "reactivation"))
        when += timedelta(minutes=int(ctx.rng.integers(5, 30)))
    return rows


def _dormant_days(
    customer: SyntheticCustomer, fraud: FraudConfig, data_end: datetime
) -> tuple[date, date] | None:
    """First and last day F8 may happen: dormant for more than ``dormancy_days``, inside the data.

    The customer's last activity of any kind (v1 or v2) is on or before ``churned_on``.
    """
    if customer.churned_on is None:
        return None
    first = customer.churned_on + timedelta(days=fraud.dormancy_days + 2)
    last = (data_end - timedelta(days=1)).date()
    return (first, last) if first <= last else None


def _later_scenarios(
    config: GeneratorConfig, fraud: FraudConfig, ctx: _Ctx, data_end: datetime
) -> list[FraudLabel]:
    """F7/F8 (v2.2) for customers without an F1-F6 incident, on their own stream, after all
    v2.1 rows exist: no earlier draw, id or row changes."""
    ctx.rng = np.random.default_rng([config.seed, ctx.customer.index, 9])
    draw, f7 = float(ctx.rng.uniform()), fraud.rates["F7"]
    dormant = _dormant_days(ctx.customer, fraud, data_end)
    rows: list[tuple[Transfer, str]] = []
    if draw < f7 and ctx.window_end - ctx.window_start >= timedelta(days=4):
        scenario: Scenario = "F7"
        rows = _f7(ctx, fraud.reporting_threshold_minor)
    elif dormant is not None and f7 <= draw < f7 + fraud.rates["F8"]:
        scenario = "F8"
        # v2.0 keeps making ordinary transfers after churn (#276; v1 and the v2.1 additions stop).
        # A dormant account has none: drop them for THIS customer only. Every other v2.1 row is
        # untouched, and 6-month datasets (training) have no F8, so they stay byte-identical.
        ctx.rows = [t for t in ctx.rows if t.occurred_at < ctx.window_end]
        rows = _f8(ctx, *dormant, fraud.max_transfer_minor)
    if not rows:
        return []
    incident = uuid.uuid5(ctx.customer.id, f"incident-{scenario}")
    return [FraudLabel(row.id, scenario, incident, role, True) for row, role in rows]


def _instant(ctx: _Ctx, rng: np.random.Generator) -> datetime:
    base = ctx.window_start + (ctx.window_end - ctx.window_start) * float(rng.uniform(0.02, 0.95))
    return base.replace(hour=int(rng.integers(8, 22)), minute=int(rng.integers(0, 60)))


def _usual(ctx: _Ctx, rng: np.random.Generator) -> int:
    return ctx.money(ctx.customer.monthly_income_minor * float(rng.uniform(0.01, 0.12)))


def _new_payees(ctx: _Ctx, rng: np.random.Generator, months: int) -> None:
    """L1: about two new payees in six months, paid minutes to three days after being added."""
    for _ in range(int(rng.poisson(0.35 * months))):
        added = _instant(ctx, rng)
        payee = ctx.party_ids.next()
        delay = rng.uniform(2, 60) if rng.uniform() < 0.5 else rng.uniform(60, 3 * 24 * 60)
        first = added + timedelta(minutes=float(delay))
        one_off = rng.uniform() < 0.15  # a deposit or a big purchase: still under F1's 1.5x income
        share = float(rng.uniform(0.15, 0.6)) if one_off else float(rng.uniform(0.01, 0.12))
        if first < ctx.window_end:
            ctx.add(
                first, "DEBIT", ctx.money(ctx.customer.monthly_income_minor * share), payee, added
            )
        for _ in range(int(rng.integers(0, 3))):  # then used again, like any payee
            later = first + timedelta(days=float(rng.uniform(5, 60)))
            if later < ctx.window_end:
                ctx.add(later, "DEBIT", _usual(ctx, rng), payee, added)


def _new_phone(ctx: _Ctx, rng: np.random.Generator) -> None:
    """L2: 12% of customers switch phones; transfers after that come from the new device."""
    if rng.uniform() >= 0.12:
        return
    switch = _instant(ctx, rng)
    phone = uuid.uuid5(ctx.customer.id, f"phone-{switch.isoformat()}")
    for _ in range(int(rng.integers(1, 4))):
        when = switch + timedelta(days=float(rng.uniform(0, 20)))
        if when < ctx.window_end:
            payee, added = ctx.payees[int(rng.integers(0, len(ctx.payees)))]
            ctx.add(when, "DEBIT", _usual(ctx, rng), payee, added, device=phone)


def _forgotten_password(ctx: _Ctx, rng: np.random.Generator, months: int) -> None:
    """L3: 1-4 failed logins, then a normal transfer from the usual device to a known payee."""
    for _ in range(int(rng.poisson(0.15 * months))):
        payee, added = ctx.payees[int(rng.integers(0, len(ctx.payees)))]
        failed = int(rng.integers(1, 5))
        ctx.add(_instant(ctx, rng), "DEBIT", _usual(ctx, rng), payee, added, failed=failed)


def _first_time_behaviour(ctx: _Ctx, rng: np.random.Generator, months: int) -> None:
    """L1-L3 (module docstring). After the scenarios, on its own stream: scenario rows unchanged."""
    if ctx.window_end - ctx.window_start < timedelta(days=2):
        return
    _new_payees(ctx, rng, months)
    _new_phone(ctx, rng)
    _forgotten_password(ctx, rng, months)


_INJECTORS = {"F1": _f1, "F2": _f2, "F3": _f3, "F4": _f4, "F5": _f5, "F6": _f6}


def _scenario_for(rng: np.random.Generator, config: FraudConfig) -> Scenario | None:
    """F1-F6 only (F7/F8 are drawn later on their own stream), so v2.1 assignments stand."""
    draw, total = float(rng.uniform()), 0.0
    for scenario in _V2_0_SCENARIOS:
        total += config.rates[scenario]
        if draw < total:
            return scenario
    return None


def customer_transfers(
    config: GeneratorConfig, fraud: FraudConfig, customer: SyntheticCustomer
) -> tuple[list[Transfer], list[FraudLabel]]:
    rng = np.random.default_rng([config.seed, customer.index, 7])
    months = [at_ist(m, 0) for m in month_starts(config.start, config.months)]
    window_start = max(months[0], at_ist(customer.joined_on, 0))
    data_end = end = months[-1] + timedelta(days=27)
    if customer.churned_on is not None:
        end = min(end, at_ist(customer.churned_on, 0))
    party_ids = _Ids(customer, "parties")
    payees = [
        (
            party_ids.next(),
            months[0] - timedelta(days=int(rng.integers(_PAYEE_MIN_DAYS, 400))),
        )
        for _ in range(int(rng.integers(1, 4)))
    ]
    ctx = _Ctx(
        customer=customer,
        rng=rng,
        window_start=window_start,
        window_end=max(end, window_start + timedelta(days=1)),
        home_city=customer.city if customer.city in {c for c, _ in CITIES} else CITIES[0][0],
        device=uuid.uuid5(customer.id, "device"),
        payees=payees,
        tx_ids=_Ids(customer, "transfers"),
        party_ids=party_ids,
    )
    _ordinary_transfers(ctx, [m for m in months if m >= window_start - timedelta(days=31)])
    labels: list[FraudLabel] = []
    scenario = _scenario_for(rng, fraud)
    if scenario is not None:
        incident = uuid.uuid5(customer.id, f"incident-{scenario}")
        labels = [
            FraudLabel(row.id, scenario, incident, role, role != "home")
            for row, role in _INJECTORS[scenario](ctx)
        ]
    first_time_rng = np.random.default_rng([config.seed, customer.index, 8])
    _first_time_behaviour(ctx, first_time_rng, config.months)
    if scenario is None:
        labels = _later_scenarios(config, fraud, ctx, data_end)
    rows = sorted(ctx.rows, key=lambda t: (t.occurred_at, t.id))
    return rows, labels


def iter_labelled(
    config: GeneratorConfig, fraud: FraudConfig
) -> Iterator[tuple[list[Transfer], list[FraudLabel]]]:
    for customer in iter_customers(config):
        yield customer_transfers(config, fraud, customer)


def generate_labelled(
    config: GeneratorConfig, fraud: FraudConfig | None = None
) -> LabelledTransfers:
    fraud = fraud or FraudConfig()
    transfers: list[Transfer] = []
    labels: list[FraudLabel] = []
    for rows, lbls in iter_labelled(config, fraud):
        transfers += rows
        labels += lbls
    return LabelledTransfers(run_id_for(config), tuple(transfers), tuple(labels))
