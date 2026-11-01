"""Fraud model training (TASK-029 LightGBM, TASK-030 Isolation Forest). Offline only.

Never imported on the request path. Data: the labelled synthetic transfers from TASK-023
(``pbip_datagen`` is passed in as plain rows, so this module does not import it). Features: the
TASK-024 offline pipeline plus amount, hour and device signals.

Splits are by TIME (train < validation < test); a random split would leak the future into
training. Classes are heavily imbalanced, so the report is precision, recall, F1, PR-AUC, FPR,
FNR and the confusion matrix - never accuracy.
"""

import math
import time
from dataclasses import asdict, dataclass
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score, confusion_matrix

from pbip_ml.fraud.features import FEATURE_NAMES, offline_features

MODEL_FEATURES = (
    *FEATURE_NAMES,
    "log_amount",
    "hour_ist",
    "new_device",
    "is_debit",
)
ANOMALY_FEATURES = ("ratio_to_avg_amount", "velocity_10m", "distinct_devices_7d", "log_amount")
LGBM_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 20,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.9,
    "bagging_freq": 1,
    "verbose": -1,
    "seed": 42,
    "deterministic": True,
    "num_threads": 1,
}


def build_dataset(transfers: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Features + label per transfer. ``labels``: transaction_id, fraudulent (True = fraud leg)."""
    df = transfers.reset_index(drop=True).copy()
    df["occurred_at"] = pd.to_datetime(df["occurred_at"], utc=True)
    features = offline_features(df)
    fraud_ids = set(labels.loc[labels["fraudulent"].astype(bool), "transaction_id"])
    out = features.copy()
    out["log_amount"] = np.log10(df["amount_minor"].astype(float))
    out["hour_ist"] = (df["occurred_at"] + pd.Timedelta(hours=5, minutes=30)).dt.hour
    out["new_device"] = df["new_device"].astype(int)
    out["is_debit"] = (df["direction"].astype(str) == "DEBIT").astype(int)
    out["amount_minor"] = df["amount_minor"].astype(
        "int64"
    )  # for the rule layer (not a model input)
    out["occurred_at"] = df["occurred_at"]
    out["label"] = df["id"].isin(fraud_ids).astype(int)
    return out


@dataclass(frozen=True)
class Split:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame


def time_split(data: pd.DataFrame, train: float = 0.6, validation: float = 0.2) -> Split:
    """Chronological cut points: every training row is earlier than every validation row, etc."""
    ordered = data.sort_values("occurred_at", kind="stable")
    t1 = ordered["occurred_at"].quantile(train)
    t2 = ordered["occurred_at"].quantile(train + validation)
    return Split(
        ordered[ordered["occurred_at"] < t1],
        ordered[(ordered["occurred_at"] >= t1) & (ordered["occurred_at"] < t2)],
        ordered[ordered["occurred_at"] >= t2],
    )


@dataclass(frozen=True)
class Metrics:
    rows: int
    positives: int
    threshold: float
    precision: float
    recall: float
    f1: float
    pr_auc: float
    fpr: float
    fnr: float
    tn: int
    fp: int
    fn: int
    tp: int

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


def evaluate(y_true: np.ndarray, scores: np.ndarray, threshold: float) -> Metrics:
    predicted = (scores >= threshold).astype(int)
    tn, fp, fn, tp = (int(v) for v in confusion_matrix(y_true, predicted, labels=[0, 1]).ravel())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return Metrics(
        rows=len(y_true),
        positives=int(y_true.sum()),
        threshold=round(threshold, 6),
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        pr_auc=round(float(average_precision_score(y_true, scores)), 4) if y_true.any() else 0.0,
        fpr=round(fp / (fp + tn), 6) if fp + tn else 0.0,
        fnr=round(fn / (fn + tp), 4) if fn + tp else 0.0,
        tn=tn,
        fp=fp,
        fn=fn,
        tp=tp,
    )


def best_f1_threshold(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Threshold maximising F1 on VALIDATION data (never chosen on the test split)."""
    candidates = np.unique(np.quantile(scores, np.linspace(0.5, 0.9999, 400)))
    best, best_f1 = 0.5, -1.0
    for threshold in candidates:
        f1 = evaluate(y_true, scores, float(threshold)).f1
        if f1 > best_f1:
            best, best_f1 = float(threshold), f1
    return best


def _xy(frame: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    return frame[list(MODEL_FEATURES)], frame["label"].to_numpy()


def train_lightgbm(split: Split, rounds: int = 600) -> tuple[lgb.Booster, float]:
    """Class-weighted LightGBM, early stopping on validation PR-AUC. Returns (model, threshold)."""
    x_train, y_train = _xy(split.train)
    x_val, y_val = _xy(split.validation)
    positives = max(int(y_train.sum()), 1)
    params = {
        **LGBM_PARAMS,
        "scale_pos_weight": (len(y_train) - positives) / positives,
        "metric": "average_precision",
    }
    booster = lgb.train(
        params,
        lgb.Dataset(x_train, y_train),
        num_boost_round=rounds,
        valid_sets=[lgb.Dataset(x_val, y_val)],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    threshold = best_f1_threshold(
        y_val, np.asarray(booster.predict(x_val, num_iteration=booster.best_iteration))
    )
    return booster, threshold


@dataclass
class AnomalyModel:
    """Isolation Forest over normal training debits, with a 0-1 score and a reason."""

    forest: IsolationForest
    low: float
    high: float
    medians: dict[str, float]
    spreads: dict[str, float]

    def score(self, frame: pd.DataFrame) -> np.ndarray:
        raw = -self.forest.score_samples(
            frame[list(ANOMALY_FEATURES)].to_numpy(dtype=float)
        )  # higher = more anomalous
        clipped: np.ndarray = np.clip((raw - self.low) / (self.high - self.low), 0.0, 1.0)
        return clipped

    def explain(self, row: pd.Series) -> str:
        """The feature furthest from typical training behaviour, in plain words."""

        def distance(name: str) -> float:
            return abs(float(row[name]) - self.medians[name]) / self.spreads[name]

        name = max(ANOMALY_FEATURES, key=distance)
        return f"{name} is {float(row[name]):.2f}, usually about {self.medians[name]:.2f}"


def train_isolation_forest(split: Split, seed: int = 42) -> AnomalyModel:
    normal = split.train[(split.train["label"] == 0) & (split.train["is_debit"] == 1)]
    x = normal[list(ANOMALY_FEATURES)]
    forest = IsolationForest(n_estimators=100, contamination="auto", random_state=seed)
    forest.fit(x.to_numpy(dtype=float))
    raw = -forest.score_samples(x.to_numpy(dtype=float))
    q1, q3 = x.quantile(0.25), x.quantile(0.75)
    spreads = {n: max(float(q3[n] - q1[n]), 1e-6) for n in ANOMALY_FEATURES}
    return AnomalyModel(
        forest,
        low=float(np.quantile(raw, 0.5)),
        high=float(np.quantile(raw, 0.999)),
        medians={n: float(x[n].median()) for n in ANOMALY_FEATURES},
        spreads=spreads,
    )


def single_prediction_ms(
    fn: Any, row: pd.DataFrame, repeats: int = 200, clock: Any = time.perf_counter
) -> dict[str, float]:
    """p50/p95 latency of scoring ONE row in memory (what the request path would do).

    Reports use wall time (``perf_counter``). Tests pass ``time.process_time`` - this process's
    CPU time - so a busy machine running other test workers cannot make them flaky.
    """
    timings = []
    for _ in range(repeats):
        started = clock()
        fn(row)
        timings.append((clock() - started) * 1000)
    timings.sort()
    return {
        "p50_ms": round(timings[len(timings) // 2], 3),
        "p95_ms": round(timings[math.ceil(len(timings) * 0.95) - 1], 3),
    }
