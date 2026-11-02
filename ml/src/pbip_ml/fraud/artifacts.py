"""What the served fraud artifacts contain, and the safe way to read the anomaly pickle (ADR-0033).

Inference-safe: no training code, no ``sklearn.metrics``. Training (``training.py``) imports the
feature order and :class:`AnomalyModel` from here, so serving never has to import training
(AGENTS.md section 2, #350).

``isolation_forest.joblib`` is a pickle, and unpickling can construct any importable object.
:func:`load_anomaly_model` reads it with a restricted unpickler that constructs ONLY the classes an
Isolation Forest artifact needs (:data:`ALLOWED_CLASSES`) and refuses everything else, so a pickle
naming ``os.system`` or any other callable is rejected before it runs. Artifacts written before
ADR-0033 name the class by its old path ``pbip_ml.fraud.training.AnomalyModel``; the unpickler maps
that one name to this module, so they still load without the training module being imported.
"""

import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from joblib.numpy_pickle import NumpyUnpickler
from sklearn.ensemble import IsolationForest

from pbip_ml.fraud.features import FEATURE_NAMES

MODEL_FEATURES = (
    *FEATURE_NAMES,
    "log_amount",
    "hour_ist",
    "new_device",
    "is_debit",
)
ANOMALY_FEATURES = ("ratio_to_avg_amount", "velocity_10m", "distinct_devices_7d", "log_amount")


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


_HERE = (__name__, "AnomalyModel")
# Exactly the globals ``joblib.dump(AnomalyModel(...))`` writes (recorded from the trained artifact
# with scikit-learn 1.9, numpy 2, joblib 1.6). A dependency upgrade that renames one of them makes
# the load fail, and the API then serves the rules alone (fail safe) until the list is reviewed.
ALLOWED_CLASSES = frozenset(
    {
        _HERE,
        ("joblib.numpy_pickle", "NumpyArrayWrapper"),
        ("numpy", "dtype"),
        ("numpy", "ndarray"),
        ("sklearn.ensemble._iforest", "IsolationForest"),
        ("sklearn.tree._classes", "ExtraTreeRegressor"),
        ("sklearn.tree._tree", "Tree"),
    }
)
# Pre-ADR-0033 artifacts: the class lived in the training module. Mapped, never imported.
LEGACY_NAMES = {("pbip_ml.fraud.training", "AnomalyModel"): _HERE}


class _AllowListUnpickler(NumpyUnpickler):  # type: ignore[misc]  # joblib ships no type hints
    """joblib's unpickler (it reads the numpy arrays stored inline), limited to the allow-list."""

    def find_class(self, module: str, name: str) -> object:
        module, name = LEGACY_NAMES.get((module, name), (module, name))
        if (module, name) not in ALLOWED_CLASSES:
            raise pickle.UnpicklingError(f"{module}.{name} is not allowed in a fraud artifact")
        return super().find_class(module, name)


def load_anomaly_model(path: Path) -> AnomalyModel:
    """Read ``isolation_forest.joblib`` (uncompressed, as ``train.py`` writes it) safely.

    Raises ``pickle.UnpicklingError`` for a forbidden class or a malformed stream, ``OSError`` if
    the file cannot be read, and ``TypeError`` if the pickle is not an :class:`AnomalyModel`.
    """
    with path.open("rb") as handle:
        loaded = _AllowListUnpickler(str(path), handle, ensure_native_byte_order=True).load()
    if not isinstance(loaded, AnomalyModel):
        raise TypeError("isolation_forest.joblib is not an AnomalyModel")
    return loaded
