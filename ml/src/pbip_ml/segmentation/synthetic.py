"""Deterministic synthetic RFM profiles for the segmentation pipeline check (no database).

Lives in the package, not in ``scripts/run_segmentation.py``, so it is type-checked with the rest of
``pbip_ml`` and tests import it without reaching into ``scripts/`` (#175).
"""

from uuid import UUID

from pbip_ml.segmentation.contract import CustomerRFMFeatures

SYNTHETIC_PERSONAS = (
    # (recency_days, frequency, monetary_minor, savings_ratio, discretionary_ratio)
    (2, 15.0, 450_000_00, 0.35, 0.28),  # high net worth
    (1, 55.0, 30_000_00, 0.15, 0.22),  # frequent transactor
    (3, 24.0, 65_000_00, 0.08, 0.72),  # discretionary spender
    (90, 0.8, 6_000_00, 0.12, 0.08),  # inactive saver
    (5, 11.0, 22_000_00, 0.42, 0.09),  # budget conscious
)


def synthetic_customers(n: int = 125) -> list[CustomerRFMFeatures]:
    """Deterministic profiles around the five personas (fixed ids, so runs are reproducible)."""
    samples: list[CustomerRFMFeatures] = []
    for index in range(n):
        recency, frequency, monetary, savings, discretionary = SYNTHETIC_PERSONAS[index % 5]
        jitter = index // 5
        samples.append(
            CustomerRFMFeatures(
                customer_id=UUID(int=index + 1),
                recency_days=recency + jitter % 4,
                frequency=round(frequency + (jitter % 6) * 0.5, 2),
                monetary_minor=monetary + jitter * 250_00,
                savings_ratio=round(savings + (jitter % 4) * 0.01, 4),
                discretionary_ratio=round(discretionary + (jitter % 3) * 0.01, 4),
            )
        )
    return samples
