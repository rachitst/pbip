"""Hybrid fraud decision: rules + calibrated LightGBM + Isolation Forest, with SHAP reasons.

TASK-031 (combiner and thresholds) and TASK-032 (reason codes). Pure scoring: no I/O, no database.

How a score is made (all parameters are fitted on the training and validation periods, never on
test data, and stored as JSON in :class:`HybridParams`, so the decision is reproducible and
auditable):

1. ``p`` = LightGBM probability, **calibrated** with isotonic regression (a lookup table) fitted on
   validation scores plus out-of-time scores from the training period (ADR-0047, #396).
2. ``a`` = Isolation Forest anomaly score in 0-1.
3. ``r`` = rule points from the TASK-025 registry (0-100).
4. Stacked probability ``sigmoid(b0 + b1*p + b2*a + b3*r/100)`` - a logistic regression fitted on
   validation: these are the "calibrated weights". Risk = round(100 * that probability).
5. **Hard-rule overrides** raise the floor: RAPID_ATO_TRANSFER -> at least REVIEW,
   NEW_PAYEE_LARGE_AMOUNT and VELOCITY_BURST -> at least STEP_UP. Rules can raise a score, never
   lower it.
6. Risk -> action: ALLOW < 30 <= STEP_UP < 60 <= REVIEW < 85 <= BLOCK.

**Fail-safe:** if the models are missing or fail, the score is the rule points alone (with a reason
saying so) - the same thresholds still apply, so the result is never "ALLOW everything".

Reasons (TASK-032): every triggered rule (RULE), the top-3 positive SHAP contributors of the
LightGBM prediction in plain words (MODEL; exact TreeSHAP from LightGBM's ``pred_contrib``, no
extra dependency), and the Isolation Forest reason when ``a >= 0.5`` (ANOMALY).
"""

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from pbip_ml.fraud.artifacts import MODEL_FEATURES, AnomalyModel
from pbip_ml.fraud.compiled_forest import CompiledForest
from pbip_ml.fraud.contract import FraudDecision, FraudReason, ReasonSource
from pbip_ml.fraud.features import FraudFeatures
from pbip_ml.fraud.rules import RuleContext, RuleRegistry, default_registry

STEP_UP_AT, REVIEW_AT, BLOCK_AT = 30, 60, 85
ANOMALY_REASON_AT = 0.5
# Inverse L2 strength of the stacked logistic regression (ADR-0047). sklearn's default 1.0 shrank
# the model weight with ~40-100 validation positives, so a fraud the model was sure of could not
# reach BLOCK. 10 had the lowest forward-in-time log-loss (stack fitted on the first half of
# validation, scored on the second half) for both the 1,500- and 3,000-customer recipes.
STACK_C = 10.0
TOP_SHAP = 3
# Rule floors: these rules are bank policy, so a model trained on synthetic data can never score
# them below their floor. VELOCITY_BURST joined in TASK-080: without it the stacked weights scored
# a 6-transfers-in-10-minutes burst at risk 0 (measured), below the STEP_UP the rule demands.
OVERRIDES: dict[str, int] = {
    "RAPID_ATO_TRANSFER": REVIEW_AT,
    "NEW_PAYEE_LARGE_AMOUNT": STEP_UP_AT,
    "VELOCITY_BURST": STEP_UP_AT,
}


def decide(risk: int) -> FraudDecision:
    if risk >= BLOCK_AT:
        return FraudDecision.BLOCK
    if risk >= REVIEW_AT:
        return FraudDecision.REVIEW
    if risk >= STEP_UP_AT:
        return FraudDecision.STEP_UP
    return FraudDecision.ALLOW


@dataclass(frozen=True)
class HybridParams:
    """Everything fitted on validation, serialisable as JSON next to the model files."""

    model_version: str
    calibration_x: list[float]
    calibration_y: list[float]
    intercept: float
    w_model: float
    w_anomaly: float
    w_rules: float

    def calibrate(self, p: np.ndarray) -> np.ndarray:
        calibrated: np.ndarray = np.interp(p, self.calibration_x, self.calibration_y)
        return calibrated

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "HybridParams":
        return cls(**json.loads(text))


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


# Natural-language templates for SHAP contributors (feature value -> analyst sentence).
_TEMPLATES: dict[str, Callable[[float], str]] = {
    "ratio_to_avg_amount": lambda v: f"amount is {v:.1f}x the customer's 30-day average",
    "payee_age_seconds": lambda v: (
        "no payee age (incoming or unknown payee)"
        if v < 0
        else f"payee was added {int(v // 60)} minutes before the transfer"
        if v < 86_400
        else f"payee was added {int(v // 86_400)} days before the transfer"
    ),
    "velocity_10m": lambda v: f"{int(v)} other transfers in the previous 10 minutes",
    "failure_count_24h": lambda v: f"{int(v)} failed sign-ins in the previous 24 hours",
    "distinct_devices_7d": lambda v: f"{int(v)} different devices used in 7 days",
    "near_threshold_72h": lambda v: (
        f"{int(v)} movements just under INR 1,00,000 in the previous 72 hours"
    ),
    "days_since_last_activity": lambda v: (
        "no earlier activity on record" if v < 0 else f"first activity in {int(v)} days"
    ),
    "log_amount": lambda v: f"amount of INR {10**v / 100:,.2f}",
    "hour_ist": lambda v: f"made at {int(v):02d}:00 IST",
    "new_device": lambda v: "from a device not seen before" if v >= 1 else "from a known device",
    "is_debit": lambda v: "money leaving the account" if v >= 1 else "money arriving",
}


def model_columns(booster: lgb.Booster) -> list[str]:
    """The columns this booster was trained on, in its order (ADR-0051).

    A model is served with the feature row of the code that serves it, which can have more
    columns than the model was trained on: the -3 artifacts (9 features) still score correctly
    after ``near_threshold_72h`` and ``days_since_last_activity`` were added. LightGBM matches
    columns by position, never by name, so the row is always cut to the booster's own list.
    """
    return list(booster.feature_name())


def model_matrix(row: pd.DataFrame, columns: Sequence[str] = MODEL_FEATURES) -> np.ndarray:
    """The LightGBM input for ``row``: exactly what ``booster.predict(DataFrame)`` builds itself.

    LightGBM converts a DataFrame with ``np.result_type(*column dtypes, float32)``, float64 for
    these int64/float64 columns, so passing this matrix gives identical predictions while skipping
    its pandas conversion (about half a millisecond per call). Built once, shared by predict and
    TreeSHAP. ``columns`` is the booster's own list (``model_columns``).
    """
    matrix: np.ndarray = row[list(columns)].to_numpy(dtype=np.float64)
    return matrix


def shap_reasons(
    booster: lgb.Booster,
    row: pd.DataFrame,
    top: int = TOP_SHAP,
    matrix: np.ndarray | None = None,
) -> list[FraudReason]:
    """Top positive TreeSHAP contributors (log-odds) for ONE row, as MODEL reasons.

    ``matrix`` is ``model_matrix(row, model_columns(booster))`` when the caller has built it
    already. ``num_threads=1``: one row needs no OpenMP pool, and on the 2-CPU laptop an
    idle-spinning second thread competes with the API under load (#379). A row's arithmetic is
    sequential either way: same values.
    """
    columns = model_columns(booster)
    data = model_matrix(row, columns) if matrix is None else matrix
    contrib = np.asarray(booster.predict(data, pred_contrib=True, num_threads=1))[0]
    values = contrib[:-1]  # last column is the expected value (bias)
    order = np.argsort(-values)
    reasons = []
    for index in order[:top]:
        if values[index] <= 0:
            break
        name = columns[index]
        reasons.append(
            FraudReason(
                code=f"MODEL_{name.upper()}",
                message=f"Model: {_TEMPLATES[name](float(row.iloc[0][name]))}",
                source=ReasonSource.MODEL,
                contribution=round(float(values[index]), 4),
            )
        )
    return reasons


@dataclass(frozen=True)
class HybridResult:
    risk_score: int
    decision: FraudDecision
    reasons: list[FraudReason]
    model_version: str
    features: dict[str, float | int | str | bool | None] = field(default_factory=dict)


@dataclass
class HybridScorer:
    """Scores one transfer from its features. ``booster``/``anomaly`` None means rules-only."""

    params: HybridParams
    booster: lgb.Booster | None
    anomaly: AnomalyModel | None
    rules: RuleRegistry = field(default_factory=default_registry)
    # The anomaly forest compiled for one-row scoring (``compiled_forest``); None = scikit-learn's.
    compiled_anomaly: CompiledForest | None = None

    def score(self, row: pd.DataFrame, context: RuleContext) -> HybridResult:
        outcome = self.rules.evaluate(context)
        reasons = list(outcome.reasons)
        snapshot: dict[str, float | int | str | bool | None] = {
            k: _plain(v) for k, v in row.iloc[0].items() if k in MODEL_FEATURES
        }
        snapshot["rule_points"] = outcome.score
        try:
            if self.booster is None or self.anomaly is None:
                raise RuntimeError("model unavailable")
            matrix = model_matrix(row, model_columns(self.booster))
            raw = float(np.asarray(self.booster.predict(matrix, num_threads=1))[0])
            p = float(self.params.calibrate(np.array([raw]))[0])
            a = float(self.anomaly.score(row, self.compiled_anomaly)[0])
        except Exception as exc:  # fail safe: rules only, never ALLOW-all
            reasons.append(
                FraudReason(
                    code="MODEL_UNAVAILABLE",
                    message="Scored by rules only: the model could not be used",
                    source=ReasonSource.RULE,
                )
            )
            snapshot["model_error"] = type(exc).__name__
            risk = outcome.score
            version = f"{self.params.model_version}+rules-only"
        else:
            z = (
                self.params.intercept
                + self.params.w_model * p
                + self.params.w_anomaly * a
                + self.params.w_rules * outcome.score / 100
            )
            risk = round(100 * _sigmoid(z))
            snapshot.update(model_probability=round(p, 4), anomaly_score=round(a, 4))
            reasons.extend(shap_reasons(self.booster, row, matrix=matrix))
            if a >= ANOMALY_REASON_AT:
                reasons.append(
                    FraudReason(
                        code="ANOMALY",
                        message=f"Unusual: {self.anomaly.explain(row.iloc[0])}",
                        source=ReasonSource.ANOMALY,
                        contribution=round(a, 4),
                    )
                )
            version = self.params.model_version
        for hit in outcome.triggered:
            risk = max(risk, OVERRIDES.get(hit.code, 0))
        risk = min(max(risk, 0), 100)
        return HybridResult(risk, decide(risk), reasons, version, snapshot)


def _plain(value: Any) -> float | int | str | bool | None:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def rule_context(row: pd.Series, currency: str = "INR") -> RuleContext:
    """RuleContext for a dataset row (training/evaluation helper)."""
    features = FraudFeatures(
        ratio_to_avg_amount=float(row["ratio_to_avg_amount"]),
        payee_age_seconds=int(row["payee_age_seconds"]),
        velocity_10m=int(row["velocity_10m"]),
        failure_count_24h=int(row["failure_count_24h"]),
        distinct_devices_7d=int(row["distinct_devices_7d"]),
        near_threshold_72h=int(row["near_threshold_72h"]),
        days_since_last_activity=int(row["days_since_last_activity"]),
    )
    return RuleContext(int(row["amount_minor"]), currency, features, bool(row["new_device"]))


def fit_params(
    validation: pd.DataFrame,
    booster: lgb.Booster,
    anomaly: AnomalyModel,
    rules: RuleRegistry,
    model_version: str,
    out_of_time: tuple[np.ndarray, np.ndarray] | None = None,
) -> HybridParams:
    """Isotonic calibration + stacked logistic weights (ADR-0047, #396).

    The calibration is fitted on the final model's validation scores PLUS ``out_of_time``
    (``training.out_of_time_scores``: earlier-period scores from fold models that never saw those
    rows), so it rests on several times more positives than validation alone. The stack is fitted
    on validation only (the anomaly model was trained on the training period). Test and held-out
    data are never used.
    """
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    y = validation["label"].to_numpy()
    raw = np.asarray(booster.predict(validation[model_columns(booster)]))
    cal_raw, cal_y = (
        (raw, y)
        if out_of_time is None
        else (np.concatenate([out_of_time[0], raw]), np.concatenate([out_of_time[1], y]))
    )
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(cal_raw, cal_y)
    p = iso.predict(raw)
    a = anomaly.score(validation)
    r = np.array([rules.evaluate(rule_context(row)).score for _, row in validation.iterrows()])
    stack = LogisticRegression(C=STACK_C, max_iter=1000).fit(np.column_stack([p, a, r / 100]), y)
    return HybridParams(
        model_version=model_version,
        calibration_x=[float(v) for v in iso.X_thresholds_],
        calibration_y=[float(v) for v in iso.y_thresholds_],
        intercept=float(stack.intercept_[0]),
        w_model=float(stack.coef_[0][0]),
        w_anomaly=float(stack.coef_[0][1]),
        w_rules=float(stack.coef_[0][2]),
    )


def batch_risk(scorer: HybridScorer, frame: pd.DataFrame) -> np.ndarray:
    """Risk scores for many rows at once (evaluation/reporting). Same arithmetic as ``score``
    without building reasons; ``test_batch_risk_matches_row_scoring`` keeps them identical."""
    rule_points = np.array(
        [scorer.rules.evaluate(rule_context(row)).score for _, row in frame.iterrows()]
    )
    floors = np.array(
        [
            max(
                [
                    OVERRIDES.get(hit.code, 0)
                    for hit in scorer.rules.evaluate(rule_context(row)).triggered
                ],
                default=0,
            )
            for _, row in frame.iterrows()
        ]
    )
    if scorer.booster is None or scorer.anomaly is None:
        risk = rule_points
    else:
        raw = np.asarray(scorer.booster.predict(frame[model_columns(scorer.booster)]))
        p = scorer.params.calibrate(raw)
        a = scorer.anomaly.score(frame)
        z = (
            scorer.params.intercept
            + scorer.params.w_model * p
            + scorer.params.w_anomaly * a
            + scorer.params.w_rules * rule_points / 100
        )
        risk = np.array([round(100 * _sigmoid(float(v))) for v in z])
    clipped: np.ndarray = np.clip(np.maximum(risk, floors), 0, 100).astype(int)
    return clipped


def score_frame(scorer: HybridScorer, frame: pd.DataFrame) -> list[HybridResult]:
    return [scorer.score(frame.iloc[[i]], rule_context(frame.iloc[i])) for i in range(len(frame))]


def decision_counts(results: list[HybridResult]) -> Mapping[str, int]:
    counts = {d.value: 0 for d in FraudDecision}
    for result in results:
        counts[result.decision.value] += 1
    return counts
