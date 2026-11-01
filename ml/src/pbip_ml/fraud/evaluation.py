"""Cost-based threshold analysis for the fraud evaluation report (TASK-039). Offline only.

A retail bank does not pick a threshold for F1; it picks the one that costs least. Every flagged
transfer costs an analyst review; every missed fraud costs the money that left. The costs are
INPUTS (stated in the report), not facts: change them and the best threshold moves.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CostPoint:
    threshold: int
    flagged: int
    missed_fraud: int
    review_cost_minor: int
    fraud_loss_minor: int

    @property
    def total_minor(self) -> int:
        return self.review_cost_minor + self.fraud_loss_minor


def cost_curve(
    labels: np.ndarray, risk: np.ndarray, amounts_minor: np.ndarray, review_cost_minor: int
) -> list[CostPoint]:
    """Expected cost at every integer threshold 0..100 (flag when ``risk >= threshold``).

    Flagged transfers each cost ``review_cost_minor``. A fraud below the threshold loses its full
    amount (no recovery assumed: the conservative case for push payments).
    """
    points = []
    for threshold in range(101):
        flagged = risk >= threshold
        missed = (labels == 1) & ~flagged
        points.append(
            CostPoint(
                threshold=threshold,
                flagged=int(flagged.sum()),
                missed_fraud=int(missed.sum()),
                review_cost_minor=int(flagged.sum()) * review_cost_minor,
                fraud_loss_minor=int(amounts_minor[missed].sum()),
            )
        )
    return points


def cheapest(points: list[CostPoint]) -> CostPoint:
    """Lowest total cost; ties go to the higher threshold (fewer customers interrupted)."""
    return min(points, key=lambda p: (p.total_minor, -p.threshold))
