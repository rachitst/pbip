"""Weighted, explainable fraud rules registry (TASK-025).

A rule is deterministic policy over the TASK-024 features: ``evaluate(context) -> RuleResult`` with
``triggered``, a ``weight`` (score points) and a human reason that cites the exact values ("Payee
added 3m ago"). New rules are added by registering an object with that method - nothing else in
the engine changes. This is the RULE layer of the hybrid scorer (TASK-033); it is side-effect free.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pbip_ml.fraud.contract import FraudReason, ReasonSource
from pbip_ml.fraud.features import UNKNOWN_PAYEE_AGE, FraudFeatures

MAX_SCORE = 100


@dataclass(frozen=True, slots=True)
class RuleContext:
    amount_minor: int
    currency: str
    features: FraudFeatures
    new_device: bool


@dataclass(frozen=True, slots=True)
class RuleResult:
    code: str
    triggered: bool
    weight: int
    reason: str = ""

    def as_reason(self) -> FraudReason:
        return FraudReason(
            code=self.code, message=self.reason, source=ReasonSource.RULE, contribution=self.weight
        )


class Rule(Protocol):
    @property
    def code(self) -> str:
        """Stable machine code, e.g. NEW_PAYEE_LARGE_AMOUNT."""
        ...

    def evaluate(self, context: RuleContext) -> RuleResult:
        """Deterministic, side-effect free; ``weight`` is 0 unless ``triggered``."""
        ...


def _money(minor: int, currency: str) -> str:
    return f"{currency} {minor // 100:,}.{minor % 100:02d}"


def _age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


@dataclass(frozen=True)
class NewPayeeLargeAmount:
    code: str = "NEW_PAYEE_LARGE_AMOUNT"
    max_payee_age_seconds: int = 24 * 3600
    min_amount_minor: int = 25_000_00
    weight: int = 45

    def evaluate(self, context: RuleContext) -> RuleResult:
        age = context.features.payee_age_seconds
        fresh = UNKNOWN_PAYEE_AGE < age < self.max_payee_age_seconds
        if not (fresh and context.amount_minor >= self.min_amount_minor):
            return RuleResult(self.code, False, 0)
        return RuleResult(
            self.code,
            True,
            self.weight,
            f"Payee added {_age(age)} ago and {_money(context.amount_minor, context.currency)} "
            f"is at least {_money(self.min_amount_minor, context.currency)}",
        )


@dataclass(frozen=True)
class VelocityBurst:
    code: str = "VELOCITY_BURST"
    min_transfers: int = 4  # including this one
    weight: int = 30

    def evaluate(self, context: RuleContext) -> RuleResult:
        count = context.features.velocity_10m + 1
        if count < self.min_transfers:
            return RuleResult(self.code, False, 0)
        return RuleResult(
            self.code, True, self.weight, f"{count} transfers from this customer within 10 minutes"
        )


@dataclass(frozen=True)
class RapidAtoTransfer:
    """Account takeover (F3): repeated failed sign-ins, then money leaves from a new device."""

    code: str = "RAPID_ATO_TRANSFER"
    min_failed_logins: int = 3
    weight: int = 50

    def evaluate(self, context: RuleContext) -> RuleResult:
        failures = context.features.failure_count_24h
        if not (failures >= self.min_failed_logins and context.new_device):
            return RuleResult(self.code, False, 0)
        return RuleResult(
            self.code,
            True,
            self.weight,
            f"{failures} failed sign-ins in the last 24h, then a transfer from a device not seen "
            "before",
        )


@dataclass(frozen=True)
class AmountSpike:
    code: str = "AMOUNT_SPIKE"
    min_ratio: float = 5.0
    weight: int = 35

    def evaluate(self, context: RuleContext) -> RuleResult:
        ratio = context.features.ratio_to_avg_amount
        if ratio < self.min_ratio:
            return RuleResult(self.code, False, 0)
        return RuleResult(
            self.code, True, self.weight, f"Amount is {ratio:.1f}x this customer's 30-day average"
        )


@dataclass(frozen=True)
class RuleOutcome:
    score: int
    triggered: tuple[RuleResult, ...]

    @property
    def reasons(self) -> list[FraudReason]:
        return [r.as_reason() for r in self.triggered]


@dataclass
class RuleRegistry:
    rules: list[Rule] = field(default_factory=list)

    def register(self, rule: Rule) -> None:
        if any(r.code == rule.code for r in self.rules):
            raise ValueError(f"rule {rule.code} is already registered")
        self.rules.append(rule)

    @property
    def codes(self) -> Sequence[str]:
        return [r.code for r in self.rules]

    def evaluate(self, context: RuleContext) -> RuleOutcome:
        results = [rule.evaluate(context) for rule in self.rules]
        hits = tuple(r for r in results if r.triggered)
        return RuleOutcome(min(sum(r.weight for r in hits), MAX_SCORE), hits)


def default_registry(extra: Iterable[Rule] = ()) -> RuleRegistry:
    registry = RuleRegistry()
    built_in: list[Rule] = [
        NewPayeeLargeAmount(),
        VelocityBurst(),
        RapidAtoTransfer(),
        AmountSpike(),
    ]
    for rule in [*built_in, *extra]:
        registry.register(rule)
    return registry
