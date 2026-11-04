"""The compiled Isolation Forest scores exactly like scikit-learn's, and the served scorer uses it.

Why it exists: ``IsolationForest.score_samples`` costs ~15 ms of fixed overhead for one row, paid by
every transfer on the request path while its account rows are locked (k6 load suite, #101).
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pbip_datagen.fraud import generate_labelled
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud import inference as inf
from pbip_ml.fraud.artifacts import ANOMALY_FEATURES, AnomalyModel
from pbip_ml.fraud.compiled_forest import CompiledForest, compile_forest
from pbip_ml.fraud.features import PastTransfer
from sklearn.ensemble import IsolationForest

pytestmark = pytest.mark.unit

RNG = np.random.default_rng(7)
TRAIN = RNG.normal(size=(2000, 4)) * np.array([1.0, 3.0, 0.5, 2.0])
# Out-of-sample rows over several magnitudes, plus values that sit exactly on float32 boundaries.
ROWS = np.vstack(
    [RNG.normal(size=(3000, 4)) * 10, TRAIN[:200], np.zeros((1, 4)), 1e30 * np.ones((1, 4))]
)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"max_features": 2},  # per-tree column subsets
        {"bootstrap": True, "n_estimators": 13},
        {"max_samples": 2},  # depth-1 trees
        {"max_samples": 1},  # the degenerate denominator scikit-learn special-cases
        {"contamination": 0.05, "max_samples": 0.5},
    ],
)
def test_compiled_scores_are_bit_identical_to_score_samples(params: dict[str, object]) -> None:
    forest = IsolationForest(random_state=3, **params).fit(TRAIN)  # type: ignore[arg-type]  # parametrised kwargs
    compiled = compile_forest(forest)
    assert isinstance(compiled, CompiledForest)
    assert np.array_equal(compiled.score_samples(ROWS), forest.score_samples(ROWS))


def test_a_forest_it_cannot_reproduce_is_not_compiled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-time proof: if the walk and scikit-learn ever disagree (an upgrade that changes the
    formula), the caller gets None and keeps scikit-learn's scores."""
    forest = IsolationForest(random_state=3).fit(TRAIN)
    honest = forest.score_samples
    monkeypatch.setattr(forest, "score_samples", lambda x: honest(x) + 1e-12)
    assert compile_forest(forest) is None


# Hang guard, not a speed bar (as #446): training the shared model (about 5 s, in this test's setup
# when it runs first) and 300 transfers each scored by scikit-learn's own score_samples (about
# 15 ms of fixed overhead a row, the cost the compiled forest exists to avoid) take about 10 s on
# an idle 2-CPU laptop; under load the 60 s pytest-timeout default fired.
@pytest.mark.timeout(240)
def test_the_served_scorer_uses_the_compiled_forest_and_decides_identically(
    artifacts: Path,
) -> None:
    served = inf.load(artifacts)
    assert served.compiled_anomaly is not None
    plain = inf.load(artifacts)
    object.__setattr__(plain, "compiled_anomaly", None)  # scikit-learn's own score_samples
    transfers = generate_labelled(GeneratorConfig(seed=9, customers=60, months=2)).transfers
    debits = [x for x in transfers if str(x.direction) == "DEBIT"][:300]
    for transfer in debits:
        history = [
            PastTransfer(h.occurred_at, str(h.direction), h.amount_minor, h.device_id)  # type: ignore[arg-type]  # generator emits DEBIT/CREDIT
            for h in transfers
            if h.customer_id == transfer.customer_id and h.occurred_at < transfer.occurred_at
        ]
        inputs = inf.TransferInputs(
            transfer.amount_minor, "INR", transfer.occurred_at, transfer.payee_added_at,
            transfer.device_id, transfer.new_device, transfer.failed_logins_24h, history,
        )  # fmt: skip
        fast, slow = inf.score_transfer(served, inputs), inf.score_transfer(plain, inputs)
        assert fast == slow  # risk, decision, reasons and the persisted feature snapshot
    assert len(debits) == 300


def test_rows_with_a_missing_value_are_scored_by_scikit_learn() -> None:
    """NaN follows scikit-learn's missing-value branch, which the walk does not model: without the
    fallback these rows would score differently (asserted first, so the test cannot pass vacuously)."""
    forest = IsolationForest(random_state=3).fit(TRAIN)
    compiled = compile_forest(forest)
    assert compiled is not None
    model = AnomalyModel(forest, 0.3, 0.8, {}, {})
    x = RNG.normal(size=(200, 4))
    x[:, 1] = np.nan
    assert not np.array_equal(compiled.score_samples(x), forest.score_samples(x))
    frame = pd.DataFrame(x, columns=list(ANOMALY_FEATURES))
    assert np.array_equal(model.score(frame, compiled), model.score(frame))
