"""Population Stability Index (PSI) drift monitor for fraud-model input features (TASK-068).

PSI compares the distribution of a feature in a *current* window with a *reference* window:

    PSI = sum over bins of (a_i - e_i) * ln(a_i / e_i)

where ``e_i`` is the share of reference values in bin ``i`` and ``a_i`` the share of current values.

Binning choice (and why):

* **Quantile bins from the reference window** (deciles by default). Cut points are the reference
  quantiles, so every reference bin holds roughly the same mass. Fixed-width bins would put almost
  all transaction amounts (heavily right-skewed) in the first bin and make PSI blind to shifts
  there. Duplicate cut points (e.g. velocities, which are small integers with many ties) are
  collapsed, so a bin is never empty by construction of the edges alone.
* Bins are right-closed: ``(-inf, c1], (c1, c2], ..., (ck, +inf)``. Open outer bins mean current
  values outside the reference range are still counted (that is exactly the drift we want to see).
* **Epsilon floor for empty bins.** ``ln(0)`` is undefined, so a bin share of 0 is replaced by
  ``EPSILON`` (1e-4) before applying the formula. The shares are *not* renormalised afterwards:
  the error is at most ``bins * EPSILON`` of mass, it keeps hand-computed expectations simple, and
  an empty bin still produces a large (finite) contribution, which is the correct signal.

Windows are half-open ``[start, end)`` and derived from an explicit ``window_end`` supplied by the
caller (an injected clock), never from the wall clock, so every function here is pure and
deterministic. Timestamps must be timezone-aware.

Thresholds (industry convention): PSI < 0.1 stable, 0.1 <= PSI <= 0.25 warning, PSI > 0.25
critical. A value exactly on 0.1 is already a warning; exactly 0.25 is still a warning.
"""

import math
from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

import numpy as np

EPSILON = 1e-4
DEFAULT_BINS = 10
DEFAULT_WINDOW_DAYS = 30
WARNING_THRESHOLD = 0.1
CRITICAL_THRESHOLD = 0.25

FEATURE_AMOUNT = "amount"
FEATURE_VELOCITY = "velocity"


class PsiStatus(StrEnum):
    STABLE = "stable"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class PsiResult:
    """PSI of one feature between the reference and current windows."""

    feature: str
    psi: float
    status: PsiStatus
    reference_size: int
    current_size: int


def classify(psi: float) -> PsiStatus:
    """Map a PSI value to its status (boundaries: 0.1 is warning, 0.25 is warning)."""
    if psi < 0 or math.isnan(psi):
        raise ValueError("PSI must be a non-negative number")
    if psi < WARNING_THRESHOLD:
        return PsiStatus.STABLE
    if psi <= CRITICAL_THRESHOLD:
        return PsiStatus.WARNING
    return PsiStatus.CRITICAL


def quantile_edges(reference: Sequence[float], bins: int = DEFAULT_BINS) -> list[float]:
    """Interior cut points: the reference quantiles at 1/bins, ..., (bins-1)/bins, de-duplicated."""
    if bins < 2:
        raise ValueError("bins must be at least 2")
    if len(reference) == 0:
        raise ValueError("reference window is empty")
    levels = np.arange(1, bins) / bins
    cuts = np.quantile(np.asarray(reference, dtype=float), levels)
    return [float(c) for c in np.unique(cuts)]


def bin_proportions(values: Sequence[float], edges: Sequence[float]) -> list[float]:
    """Share of ``values`` in each right-closed bin defined by the interior ``edges``."""
    if len(values) == 0:
        raise ValueError("cannot bin an empty window")
    idx = np.searchsorted(np.asarray(edges, dtype=float), np.asarray(values, dtype=float), "left")
    counts = np.bincount(idx, minlength=len(edges) + 1)
    return [float(c) / len(values) for c in counts]


def psi_from_proportions(
    expected: Sequence[float], actual: Sequence[float], epsilon: float = EPSILON
) -> float:
    """PSI formula on bin shares; zero shares are floored at ``epsilon`` (see module docstring)."""
    if len(expected) != len(actual) or len(expected) == 0:
        raise ValueError("expected and actual must be non-empty and of equal length")
    total = 0.0
    for e_raw, a_raw in zip(expected, actual, strict=True):
        e, a = max(e_raw, epsilon), max(a_raw, epsilon)
        total += (a - e) * math.log(a / e)
    return total


def population_stability_index(
    reference: Sequence[float], current: Sequence[float], bins: int = DEFAULT_BINS
) -> float:
    """PSI of ``current`` against ``reference`` using quantile bins of the reference."""
    edges = quantile_edges(reference, bins)
    return psi_from_proportions(bin_proportions(reference, edges), bin_proportions(current, edges))


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")


def in_window(timestamps: Sequence[datetime], start: datetime, end: datetime) -> list[int]:
    """Indices of ``timestamps`` that fall in the half-open window ``[start, end)``."""
    _require_aware(start)
    _require_aware(end)
    selected: list[int] = []
    for i, moment in enumerate(timestamps):
        _require_aware(moment)
        if start <= moment < end:
            selected.append(i)
    return selected


def daily_velocities(
    entity_keys: Sequence[Hashable], timestamps: Sequence[datetime], indices: Sequence[int]
) -> list[float]:
    """Transactions per (entity, UTC calendar day) for the selected rows.

    Velocity is the per-entity daily transaction count, the same notion the fraud rules use; only
    (entity, day) pairs with at least one transaction appear, so idle days do not dilute the shape.
    Entity keys are opaque and never leave this function.
    """
    counts: Counter[tuple[Hashable, date]] = Counter()
    for i in indices:
        day = timestamps[i].astimezone(UTC).date()
        counts[(entity_keys[i], day)] += 1
    return [float(counts[key]) for key in sorted(counts, key=_sort_key)]


def _sort_key(key: tuple[Hashable, date]) -> str:
    # Deterministic order regardless of hash seed; PSI itself is order-independent.
    return repr(key)


def drift_report(
    amounts: Sequence[int],
    entity_keys: Sequence[Hashable],
    timestamps: Sequence[datetime],
    window_end: datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    bins: int = DEFAULT_BINS,
) -> list[PsiResult]:
    """PSI for amount and velocity: current ``[end-N, end)`` vs reference ``[end-2N, end-N)``.

    ``amounts`` are integer minor units (AGENTS.md section 4); the three sequences are parallel.
    Raises ``ValueError`` if either window has no transactions (PSI is undefined, not zero).
    """
    if not len(amounts) == len(entity_keys) == len(timestamps):
        raise ValueError("amounts, entity_keys and timestamps must have equal length")
    if window_days < 1:
        raise ValueError("window_days must be at least 1")
    span = timedelta(days=window_days)
    current_idx = in_window(timestamps, window_end - span, window_end)
    reference_idx = in_window(timestamps, window_end - 2 * span, window_end - span)
    if not reference_idx or not current_idx:
        raise ValueError("both the reference and the current window need transactions")
    features = {
        FEATURE_AMOUNT: (
            [float(amounts[i]) for i in reference_idx],
            [float(amounts[i]) for i in current_idx],
        ),
        FEATURE_VELOCITY: (
            daily_velocities(entity_keys, timestamps, reference_idx),
            daily_velocities(entity_keys, timestamps, current_idx),
        ),
    }
    results: list[PsiResult] = []
    for name, (reference, current) in features.items():
        psi = population_stability_index(reference, current, bins)
        results.append(PsiResult(name, psi, classify(psi), len(reference), len(current)))
    return results
