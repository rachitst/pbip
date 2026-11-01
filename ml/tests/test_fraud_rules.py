"""TASK-025: rules registry - trigger conditions, edge cases, reason strings, extensibility, speed."""

import time
from dataclasses import dataclass, replace

import pytest
from pbip_ml.fraud.contract import ReasonSource
from pbip_ml.fraud.features import FraudFeatures
from pbip_ml.fraud.rules import (
    AmountSpike,
    NewPayeeLargeAmount,
    RapidAtoTransfer,
    RuleContext,
    RuleResult,
    VelocityBurst,
    default_registry,
)

QUIET = FraudFeatures(
    ratio_to_avg_amount=1.0,
    payee_age_seconds=30 * 86_400,
    velocity_10m=0,
    failure_count_24h=0,
    distinct_devices_7d=1,
)


def ctx(amount: int = 1_000_00, new_device: bool = False, **features: object) -> RuleContext:
    return RuleContext(amount, "INR", replace(QUIET, **features), new_device)  # type: ignore[arg-type]  # test helper


def test_quiet_transfer_triggers_nothing() -> None:
    outcome = default_registry().evaluate(ctx())
    assert outcome.score == 0 and outcome.triggered == ()


@pytest.mark.parametrize(
    ("age", "amount", "fires"),
    [
        (180, 25_000_00, True),  # exactly the threshold amount
        (180, 24_999_99, False),
        (24 * 3600 - 1, 30_000_00, True),
        (24 * 3600, 30_000_00, False),  # 24h old is no longer new
        (-1, 30_000_00, False),  # unknown payee age (credit / no payee)
    ],
)
def test_new_payee_large_amount(age: int, amount: int, fires: bool) -> None:
    result = NewPayeeLargeAmount().evaluate(ctx(amount, payee_age_seconds=age))
    assert result.triggered is fires and result.weight == (45 if fires else 0)


def test_reason_strings_cite_the_exact_values() -> None:
    assert NewPayeeLargeAmount().evaluate(ctx(30_000_00, payee_age_seconds=180)).reason == (
        "Payee added 3m ago and INR 30,000.00 is at least INR 25,000.00"
    )
    assert (
        NewPayeeLargeAmount()
        .evaluate(ctx(30_000_00, payee_age_seconds=7_200))
        .reason.startswith("Payee added 2h ago")
    )
    assert VelocityBurst().evaluate(ctx(velocity_10m=4)).reason == (
        "5 transfers from this customer within 10 minutes"
    )
    assert RapidAtoTransfer().evaluate(ctx(new_device=True, failure_count_24h=4)).reason == (
        "4 failed sign-ins in the last 24h, then a transfer from a device not seen before"
    )
    assert AmountSpike().evaluate(ctx(ratio_to_avg_amount=6.25)).reason == (
        "Amount is 6.2x this customer's 30-day average"
    )


@pytest.mark.parametrize(("prior", "fires"), [(2, False), (3, True), (10, True)])
def test_velocity_burst_counts_this_transfer(prior: int, fires: bool) -> None:
    assert VelocityBurst().evaluate(ctx(velocity_10m=prior)).triggered is fires


@pytest.mark.parametrize(
    ("failures", "new_device", "fires"),
    [(3, True, True), (2, True, False), (5, False, False), (0, True, False)],
)
def test_rapid_ato_transfer_needs_failures_and_a_new_device(
    failures: int, new_device: bool, fires: bool
) -> None:
    result = RapidAtoTransfer().evaluate(ctx(new_device=new_device, failure_count_24h=failures))
    assert result.triggered is fires


def test_scores_add_up_cap_at_100_and_become_rule_reasons() -> None:
    outcome = default_registry().evaluate(
        ctx(
            50_000_00,
            True,
            payee_age_seconds=60,
            velocity_10m=5,
            failure_count_24h=6,
            ratio_to_avg_amount=9.0,
        )
    )
    expected = {"NEW_PAYEE_LARGE_AMOUNT", "VELOCITY_BURST", "RAPID_ATO_TRANSFER", "AMOUNT_SPIKE"}
    assert {r.code for r in outcome.triggered} == expected
    assert outcome.score == 100  # 45 + 30 + 50 + 35 capped
    assert all(r.source is ReasonSource.RULE and r.contribution for r in outcome.reasons)


def test_registry_is_extensible_and_rejects_duplicates() -> None:
    @dataclass(frozen=True)
    class NightOwl:
        code: str = "NIGHT_OWL"

        def evaluate(self, context: RuleContext) -> RuleResult:
            return RuleResult(self.code, True, 5, "custom rule")

    registry = default_registry(extra=[NightOwl()])
    assert registry.codes[-1] == "NIGHT_OWL"
    assert registry.evaluate(ctx()).score == 5
    with pytest.raises(ValueError, match="already registered"):
        registry.register(VelocityBurst())


def test_evaluation_p95_is_under_15ms() -> None:
    registry = default_registry()
    contexts = [ctx(i * 1_000_00, i % 2 == 0, velocity_10m=i % 7) for i in range(1, 501)]
    timings = []
    for c in contexts:
        started = time.perf_counter()
        registry.evaluate(c)
        timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    assert timings[int(len(timings) * 0.95)] < 15
