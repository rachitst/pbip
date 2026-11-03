"""Exact single-transfer Isolation Forest scoring without scikit-learn's per-call overhead.

``IsolationForest.score_samples`` on ONE row costs 15-20 ms on the reference laptop (measured,
2-CPU Celeron), almost all of it fixed overhead: input validation, a joblib ``Parallel`` dispatch
and one Cython ``apply`` call per tree, for 100 trees. A transfer is scored on the request path
while the payer's and payee's account rows are locked, so that overhead was paid by every
transfer and by every transfer queued behind it (k6 load suite, issue #101). Walking the same
trees in plain Python is ~0.1 ms per row.

The result is **bit-identical** to ``score_samples``, not an approximation: the walk uses the
fitted trees' own split features and thresholds, the input is rounded to float32 first exactly as
scikit-learn's validation does, and the per-tree depths are summed in the same order with the same
float64 arithmetic. :func:`compile_forest` proves that on a deterministic probe set before it
returns, and returns ``None`` on any mismatch (for example a scikit-learn upgrade that changed the
formula), so the caller keeps using ``score_samples``. Only public tree attributes are read; the
path-length correction is the published formula (Liu et al. 2008), as scikit-learn implements it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest

_PROBE_ROWS = 512
_PROBE_SEED = 20261104


def _average_path_length(n: int) -> float:
    """c(n): the mean path length of an unsuccessful search in a binary tree of n samples."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1.0) + np.euler_gamma) - 2.0 * (n - 1.0) / n


@dataclass(frozen=True, slots=True)
class _Tree:
    left: list[int]
    right: list[int]
    feature: list[int]  # index into the forest's input columns; -1 at a leaf
    threshold: list[float]
    leaf_value: list[float]  # depth + c(samples in the leaf) - 1, as scikit-learn adds it


def _node_depths(left: list[int], right: list[int]) -> list[float]:
    """Number of nodes on the path from the root, root = 1 (``Tree.compute_node_depths``)."""
    depths = [0.0] * len(left)
    depths[0] = 1.0
    for node in range(len(left)):  # children always have a larger index than their parent
        if left[node] != -1:
            depths[left[node]] = depths[node] + 1.0
            depths[right[node]] = depths[node] + 1.0
    return depths


@dataclass(frozen=True, slots=True)
class CompiledForest:
    """A fitted Isolation Forest as plain lists, scored one row at a time."""

    trees: tuple[_Tree, ...]
    denominator: float
    n_features: int

    def score_samples(self, x: np.ndarray) -> np.ndarray:
        """Same contract and values as ``IsolationForest.score_samples`` (2-D input)."""
        rows = np.asarray(x, dtype=np.float32)
        return np.array([self._score_row(row) for row in rows], dtype=np.float64)

    def _score_row(self, row: np.ndarray) -> float:
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
        if self.denominator == 0:  # one training sample: scikit-learn sets the ratio to 1
            return -0.5
        return float(-(2.0 ** (-depth / self.denominator)))


def _compile(forest: IsolationForest) -> CompiledForest:
    n_features = int(forest.n_features_in_)
    trees = []
    for estimator, columns in zip(forest.estimators_, forest.estimators_features_, strict=True):
        tree = estimator.tree_
        # scikit-learn indexes the columns per tree only when it subsampled features; otherwise
        # the tree was fitted on, and is applied to, the input columns as they are.
        mapping = (
            [int(c) for c in columns] if len(columns) != n_features else list(range(n_features))
        )
        left = [int(v) for v in tree.children_left]
        right = [int(v) for v in tree.children_right]
        depths = _node_depths(left, right)
        samples = [int(v) for v in tree.n_node_samples]
        trees.append(
            _Tree(
                left=left,
                right=right,
                feature=[mapping[f] if f >= 0 else -1 for f in tree.feature],
                threshold=[float(v) for v in tree.threshold],
                leaf_value=[
                    d + _average_path_length(n) - 1.0 for d, n in zip(depths, samples, strict=True)
                ],
            )
        )
    denominator = len(forest.estimators_) * _average_path_length(int(forest.max_samples_))
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
