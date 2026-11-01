"""TASK-080: behaviour-regression suite for the hybrid fraud scorer.

Canonical transfers with known outcomes, scored by models trained exactly as in production (seeded,
deterministic). Two layers of protection:

1. **Fixed bank criteria:** a known APP scam scores >= 80; a routine grocery payment <= 25 (ALLOW);
   a velocity burst at least STEP_UP; an account takeover at least REVIEW.
2. **No silent regression:** ``data/fraud_behaviour_baseline.json`` records each known-fraud case's
   score and decision when the suite was written. A model, feature or rule change that LOWERS any of
   them fails CI. Raising is fine, and the file is updated deliberately in that PR.
   ``TOLERANCE`` absorbs only floating-point differences between CPUs (LightGBM on AVX vs no AVX).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pbip_datagen.__main__ import _export_fraud
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud import hybrid as h
from pbip_ml.fraud import training as t
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.features import PastTransfer
from pbip_ml.fraud.inference import TransferInputs, score_transfer
from pbip_ml.fraud.train import load_export

BASELINE = Path(__file__).parent / "data" / "fraud_behaviour_baseline.json"
TOLERANCE = 2
ORDER = [FraudDecision.ALLOW, FraudDecision.STEP_UP, FraudDecision.REVIEW, FraudDecision.BLOCK]

T = datetime(2026, 6, 15, 13, 0, tzinfo=UTC)  # 18:30 IST, an ordinary evening
PHONE = UUID("00000000-0000-4000-8000-000000000001")
LAPTOP = UUID("00000000-0000-4000-8000-000000000002")
# A settled customer: 20 evening debits of INR 2,000 over the last 20 days, all from the phone.
HISTORY = tuple(
    PastTransfer(T - timedelta(days=i, minutes=3), "DEBIT", 2_000_00, PHONE) for i in range(1, 21)
)
OLD_PAYEE = T - timedelta(days=200)


def _case(
    amount: int,
    payee_added_at: datetime | None = OLD_PAYEE,
    failed_logins: int = 0,
    device: UUID = PHONE,
    extra: tuple[PastTransfer, ...] = (),
) -> TransferInputs:
    return TransferInputs(
        amount_minor=amount,
        currency="INR",
        occurred_at=T,
        payee_added_at=payee_added_at,
        device_id=device,
        new_device=device != PHONE,
        failed_logins_24h=failed_logins,
        history=(*HISTORY, *extra),
    )


CASES = {
    # F1 authorised push-payment scam: payee added 15 minutes ago, 22x the usual amount.
    "app_scam": _case(45_000_00, payee_added_at=T - timedelta(minutes=15)),
    # F2 velocity burst: five transfers in the last 8 minutes, then this one.
    "velocity_burst": _case(
        3_000_00,
        extra=tuple(
            PastTransfer(T - timedelta(minutes=m), "DEBIT", 3_000_00, PHONE)
            for m in (2, 3, 5, 6, 8)
        ),
    ),
    # F3 account takeover: four failed sign-ins, then money leaves from a device never seen.
    "account_takeover": _case(20_000_00, failed_logins=4, device=LAPTOP),
    # Routine: the usual grocery amount to a long-standing payee, from the usual phone.
    "routine_grocery": _case(1_800_00),
}
KNOWN_FRAUD = ("app_scam", "velocity_burst", "account_takeover")


@pytest.fixture(scope="module")
def scorer(tmp_path_factory: pytest.TempPathFactory) -> h.HybridScorer:
    out = tmp_path_factory.mktemp("regression")
    _export_fraud(GeneratorConfig(seed=42, customers=1500, months=6), out)
    split = t.time_split(load_export(out))
    booster, _ = t.train_lightgbm(split)
    anomaly = t.train_isolation_forest(split)
    rules = h.default_registry()
    return h.HybridScorer(
        h.fit_params(split.validation, booster, anomaly, rules, "regression"),
        booster,
        anomaly,
        rules,
    )


@pytest.fixture(scope="module")
def results(scorer: h.HybridScorer) -> dict[str, h.HybridResult]:
    return {name: score_transfer(scorer, case) for name, case in CASES.items()}


def test_known_app_scam_scores_at_least_80(results: dict[str, h.HybridResult]) -> None:
    assert results["app_scam"].risk_score >= 80
    assert results["app_scam"].decision in (FraudDecision.REVIEW, FraudDecision.BLOCK)


def test_routine_grocery_payment_scores_at_most_25(results: dict[str, h.HybridResult]) -> None:
    assert results["routine_grocery"].risk_score <= 25
    assert results["routine_grocery"].decision is FraudDecision.ALLOW


def test_velocity_burst_triggers_step_up_or_higher(results: dict[str, h.HybridResult]) -> None:
    result = results["velocity_burst"]
    assert ORDER.index(result.decision) >= ORDER.index(FraudDecision.STEP_UP)
    assert "VELOCITY_BURST" in {r.code for r in result.reasons}


def test_account_takeover_is_held_for_review(results: dict[str, h.HybridResult]) -> None:
    result = results["account_takeover"]
    assert ORDER.index(result.decision) >= ORDER.index(FraudDecision.REVIEW)
    assert "RAPID_ATO_TRANSFER" in {r.code for r in result.reasons}


@pytest.mark.parametrize("name", KNOWN_FRAUD)
def test_no_change_lowers_a_known_fraud_score(
    results: dict[str, h.HybridResult], name: str
) -> None:
    recorded = json.loads(BASELINE.read_text(encoding="utf-8"))[name]
    result = results[name]
    assert result.risk_score >= recorded["risk_score"] - TOLERANCE, (name, result.risk_score)
    assert ORDER.index(result.decision) >= ORDER.index(FraudDecision(recorded["decision"])), name
