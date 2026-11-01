"""TASK-039: cost-curve arithmetic used by the evaluation report."""

import numpy as np
from pbip_ml.fraud.evaluation import cheapest, cost_curve


def test_cost_curve_counts_reviews_and_missed_fraud_exactly() -> None:
    labels = np.array([1, 1, 0, 0, 0])
    risk = np.array([90, 40, 70, 10, 0])
    amounts = np.array([50_000_00, 20_000_00, 1_000_00, 500_00, 100_00])
    points = cost_curve(labels, risk, amounts, review_cost_minor=200_00)
    at_60 = points[60]  # flags 90 and 70; misses the 40-point fraud
    assert (at_60.flagged, at_60.missed_fraud) == (2, 1)
    assert at_60.review_cost_minor == 400_00 and at_60.fraud_loss_minor == 20_000_00
    at_0 = points[0]  # everything reviewed, nothing lost
    assert at_0.total_minor == 5 * 200_00 and at_0.fraud_loss_minor == 0
    assert points[100].fraud_loss_minor == 70_000_00  # nothing flagged: every fraud lost
    best = cheapest(points)
    assert best.missed_fraud == 0 and best.flagged == 3 and 40 >= best.threshold > 10
