"""Exact single-transfer Isolation Forest scoring without scikit-learn's per-call overhead.

``IsolationForest.score_samples`` on ONE row costs 15-20 ms on the reference laptop (measured,
2-CPU Celeron), almost all of it fixed overhead: input validation, a joblib ``Parallel`` dispatch
and one Cython ``apply`` call per tree, for 100 trees. A transfer is scored on the request path
while the payer's and payee's account rows are locked, so that overhead was paid by every
transfer and by every transfer queued behind it (k6 load suite, issue #101). Walking the same
trees in plain Python is ~0.1 ms per row.

The result is **bit-identical** to ``score_samples``, not an approximation: the walk uses the
fitted trees' own split features and thresholds, the input is rounded to float32 first exactly as
scikit-learn's validation does, each leaf adds the per-tree path lengths scikit-learn itself stored
at fit time, the depths are summed in the same order, and the final ``2 ** -(depth / c(n))`` is the
same NumPy expression over the same array. (Recomputing the logarithms or the power in plain Python
is NOT exact: NumPy's vectorised ``log``/``pow`` differ from libm by one ulp on CPUs with AVX-512,
which the CI runners have.) :func:`compile_forest` proves equality on a deterministic probe set
before it returns, and returns ``None`` on any mismatch or missing attribute (for example after a
scikit-learn upgrade), so the caller keeps using ``score_samples``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.ensemble._iforest import _average_path_length

_PROBE_ROWS = 512
_PROBE_SEED = 20261104


@dataclass(frozen=True, slots=True)
class _Tree:
    left: list[int]
    right: list[int]
    feature: list[int]  # index into the forest's input columns; -1 at a leaf
    threshold: list[float]
    leaf_value: list[float]  # scikit-learn's stored depth + c(samples in the leaf) - 1


@dataclass(frozen=True, slots=True)
class CompiledForest:
    """A fitted Isolation Forest as plain lists, scored one row at a time."""

    trees: tuple[_Tree, ...]
    denominator: np.ndarray  # shape (1,), as scikit-learn computes it
    n_features: int

    def score_samples(self, x: np.ndarray) -> np.ndarray:
        """Same contract and values as ``IsolationForest.score_samples`` (2-D input)."""
        rows = np.asarray(x, dtype=np.float32)
        depths = np.array([self._depth(row) for row in rows], dtype=np.float64)
        # scikit-learn's own final step, verbatim (one training sample: ratio set to 1).
        scores = 2 ** (
            -np.divide(
                depths, self.denominator, out=np.ones_like(depths), where=self.denominator != 0
            )
        )
        return np.asarray(-scores, dtype=np.float64)

    def _depth(self, row: np.ndarray) -> float:
        values = [float(v) for v in row]
        depth = 0.0
        for tree in self.trees:
            node = 0
            while tree.left[node] != -1:
                if values[tree.feature[node]] <= tree.threshold[node]:
                    node = tree.left[node]
                else:
                    node = tree.right[node]
            depth += tree.leaf_value[node]
        return depth


def _compile(forest: IsolationForest) -> CompiledForest:
    n_features = int(forest.n_features_in_)
    trees = []
    per_tree = zip(
        forest.estimators_,
        forest.estimators_features_,
        forest._decision_path_lengths,  # what score_samples adds per leaf, stored at fit time
        forest._average_path_length_per_tree,
        strict=True,
    )
    for estimator, columns, path_lengths, average_lengths in per_tree:
        tree = estimator.tree_
        # scikit-learn indexes the columns per tree only when it subsampled features; otherwise
        # the tree was fitted on, and is applied to, the input columns as they are.
        mapping = (
            [int(c) for c in columns] if len(columns) != n_features else list(range(n_features))
        )
        trees.append(
            _Tree(
                left=[int(v) for v in tree.children_left],
                right=[int(v) for v in tree.children_right],
                feature=[mapping[f] if f >= 0 else -1 for f in tree.feature],
                threshold=[float(v) for v in tree.threshold],
                leaf_value=[float(v) for v in path_lengths + average_lengths - 1.0],
            )
        )
    denominator = len(forest.estimators_) * _average_path_length([forest._max_samples])
    return CompiledForest(tuple(trees), denominator, n_features)


def _probe(n_features: int) -> np.ndarray:
    """Deterministic rows over several magnitudes, so every tree's splits are crossed."""
    rng = np.random.default_rng(_PROBE_SEED)
    scale = rng.choice([0.01, 0.1, 1.0, 10.0, 1000.0], size=(_PROBE_ROWS, n_features))
    return rng.normal(size=(_PROBE_ROWS, n_features)) * scale


def compile_forest(forest: IsolationForest) -> CompiledForest | None:
    """The compiled forest, or ``None`` when it does not reproduce ``score_samples`` exactly."""
    try:
        compiled = _compile(forest)
        probe = _probe(compiled.n_features)
        if not np.array_equal(compiled.score_samples(probe), forest.score_samples(probe)):
            return None
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    return compiled
