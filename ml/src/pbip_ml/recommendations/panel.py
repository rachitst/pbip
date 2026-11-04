"""Turn the SIMULATED uptake panel (``pbip_datagen.uptake``, exported as CSV) into ranking rows.

Offline only (import-linter keeps it off the request path). The panel holds what the bank's data
would hold per customer-month (income, spending, lifestyle spending, EMI, balance, holdings) plus
the simulated clicks and uptakes. This module:

1. derives, at the end of each month ``t``, the ``CustomerRecommendationContext`` the API would
   build from months ``<= t`` only (three-month averages, as the analytics module does);
2. applies the eligibility and held-product rules FIRST: only the products they allow become
   candidate rows (the bank can only recommend what its rules allow);
3. labels each candidate with the simulated uptake during month ``t + 1``.

The model never sees the panel's clicks, persona or age band: the served context carries none of
them (ADR-0015 keeps the age band out of ranking; clickstream features are not in the context).
They drive the simulated labels, so for every ranker they are signal it cannot observe.

``rules-v1``'s score is computed here for comparison only, AFTER the labels exist in the panel;
nothing here feeds back into the labels (they come from ``pbip_datagen``, which imports nothing
from ``pbip_ml``).
"""

import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID, uuid5

import numpy as np

from pbip_ml.recommendations.contract import (
    CustomerRecommendationContext,
    HeldProduct,
    HeldProductKind,
)
from pbip_ml.recommendations.engine import (
    CREDIT_LINE_ID,
    FAMILY_COVER_ID,
    FIXED_DEPOSIT_ID,
    INDEX_SIP_ID,
    PRODUCT_CATALOGUE,
    REWARDS_CARD_ID,
    SAVINGS_ACCOUNT_ID,
    evaluate_eligibility,
    held_product_rule,
    relevance_score,
)
from pbip_ml.recommendations.propensity import FEATURE_NAMES, feature_vector

PANEL_FILE: Final = "uptake_panel.csv"
SUMMARY_FILE: Final = "uptake_summary.json"
HISTORY_MONTHS: Final = 3
# The data-gen product keys, mapped to the catalogue (a test checks they cover it exactly).
PRODUCT_KEYS: Final[dict[str, UUID]] = {
    "savings_account": SAVINGS_ACCOUNT_ID,
    "fixed_deposit": FIXED_DEPOSIT_ID,
    "credit_line": CREDIT_LINE_ID,
    "rewards_card": REWARDS_CARD_ID,
    "index_sip": INDEX_SIP_ID,
    "family_cover": FAMILY_COVER_ID,
}
_INDEX: Final = {product.product_id: i for i, product in enumerate(PRODUCT_CATALOGUE)}
_BY_ID: Final = {product.product_id: product for product in PRODUCT_CATALOGUE}
_NAMESPACE: Final = UUID("2b0c3f5e-41a7-4f0e-8d0a-7c6a9e4b1d22")


@dataclass(frozen=True, slots=True)
class Month:
    """The columns the bank would have. Clicks, persona and age band are deliberately not read."""

    customer: int
    month: int
    income_minor: int
    outgoings_minor: int  # living spending + EMI, as the ledger's debits
    discretionary_minor: int
    emi_minor: int
    balance_minor: int
    held: tuple[HeldProduct, ...]
    took: frozenset[UUID]


def _held(raw: str) -> tuple[HeldProduct, ...]:
    if not raw:
        return ()
    out: list[HeldProduct] = []
    for item in raw.split(";"):
        kind, amount = item.split(":")
        out.append(HeldProduct(kind=HeldProductKind(kind), amount_minor=int(amount)))
    return tuple(out)


def read_panel(directory: Path) -> dict[int, list[Month]]:
    """Per customer, the months in order."""
    panel: dict[int, list[Month]] = defaultdict(list)
    with (directory / PANEL_FILE).open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            panel[int(raw["customer"])].append(
                Month(
                    customer=int(raw["customer"]),
                    month=int(raw["month"]),
                    income_minor=int(raw["income_minor"]),
                    outgoings_minor=int(raw["spend_minor"]) + int(raw["emi_minor"]),
                    discretionary_minor=int(raw["discretionary_minor"]),
                    emi_minor=int(raw["emi_minor"]),
                    balance_minor=int(raw["balance_minor"]),
                    held=_held(raw["held"]),
                    took=frozenset(
                        pid for key, pid in PRODUCT_KEYS.items() if raw[f"took_{key}"] == "1"
                    ),
                )
            )
    for months in panel.values():
        months.sort(key=lambda m: m.month)
    return dict(panel)


def _health(savings: float, buffer: float, emi_ratio: float, income_cv: float) -> int:
    """A simplified 0-100 score with the analytics components (savings rate, buffer, debt,
    income stability), 25 points each. An approximation, not the API's exact formula."""
    points = 25 * min(max(savings, 0.0) / 0.20, 1.0)
    points += 25 * min(buffer / 6.0, 1.0)
    points += 25 * (1.0 - min(max(emi_ratio - 0.20, 0.0) / 0.40, 1.0))
    points += 25 * (1.0 - min(income_cv / 0.40, 1.0))
    return round(points)


def _segment(inflow: float, balance: float, discretionary: float, savings: float, cv: float) -> str:
    """A stand-in for the weekly k-means labels, from the behaviour each label describes."""
    if inflow >= 150_000_00 or balance >= 10_00_000_00:
        return "HIGH_NET_WORTH"
    if discretionary >= 0.38:
        return "DISCRETIONARY_SPENDER"
    if savings >= 0.15:
        return "BUDGET_CONSCIOUS"
    if cv >= 0.15:
        return "FREQUENT_TRANSACTOR"
    return "INACTIVE_SAVER"


def _cv(values: list[int]) -> float:
    mean = sum(values) / len(values)
    if mean <= 0:
        return 1.0
    return math.sqrt(sum((x - mean) ** 2 for x in values) / len(values)) / mean


def context_at(history: list[Month], t: int) -> CustomerRecommendationContext:
    """The context at the end of month ``t``, from months ``<= t`` only."""
    window = history[t - HISTORY_MONTHS + 1 : t + 1]
    inflow = sum(m.income_minor for m in window) / len(window)
    outgoings = sum(m.outgoings_minor for m in window) / len(window)
    discretionary = sum(m.discretionary_minor for m in window) / max(outgoings * len(window), 1.0)
    savings = (inflow - outgoings) / inflow if inflow > 0 else 0.0
    now = history[t]
    buffer = now.balance_minor / outgoings if outgoings > 0 else 0.0
    emi_ratio = now.emi_minor / inflow if inflow > 0 else 0.0
    cv = _cv([m.income_minor for m in history[max(t - 5, 0) : t + 1]])
    return CustomerRecommendationContext(
        customer_id=uuid5(_NAMESPACE, str(now.customer)),
        segment=_segment(inflow, now.balance_minor, discretionary, savings, cv),
        health_score=_health(savings, buffer, emi_ratio, cv),
        monthly_inflow_minor=round(inflow),
        monthly_spend_minor=round(outgoings),
        savings_ratio=round(savings, 4),
        discretionary_ratio=round(min(max(discretionary, 0.0), 1.0), 4),
        buffer_months=round(buffer, 2),
        emi_ratio=round(emi_ratio, 4),
        total_balance_minor=now.balance_minor,
        held_products=now.held,
    )  # age_band stays "unknown": the simulated customers gave no consent (ADR-0015)


def candidates(ctx: CustomerRecommendationContext) -> tuple[UUID, ...]:
    """Eligibility and held-product rules first: only these products are ever ranked."""
    return tuple(
        product.product_id
        for product in PRODUCT_CATALOGUE
        if evaluate_eligibility(product, ctx) and held_product_rule(product, ctx)[0]
    )


@dataclass(frozen=True, slots=True)
class Rows:
    """Parallel arrays, one entry per (customer-month, candidate product), grouped contiguously
    by customer-month. ``contexts[g]`` is the context of group ``g``."""

    group: np.ndarray
    customer: np.ndarray
    month: np.ndarray  # feature month (the label is the month after)
    product: np.ndarray  # index into PRODUCT_CATALOGUE
    features: np.ndarray
    rules_score: np.ndarray
    label: np.ndarray
    contexts: tuple[CustomerRecommendationContext, ...]

    def take(self, mask: np.ndarray) -> "Rows":
        return Rows(
            self.group[mask],
            self.customer[mask],
            self.month[mask],
            self.product[mask],
            self.features[mask],
            self.rules_score[mask],
            self.label[mask],
            self.contexts,
        )


def build_rows(panel: dict[int, list[Month]]) -> Rows:
    group, customer, month, product, features, rules, label = [], [], [], [], [], [], []
    contexts: list[CustomerRecommendationContext] = []
    for index in sorted(panel):
        history = panel[index]
        for t in range(HISTORY_MONTHS - 1, len(history) - 1):
            ctx = context_at(history, t)
            allowed = candidates(ctx)
            if not allowed:
                continue
            vector = feature_vector(ctx)
            taken = history[t + 1].took
            for pid in allowed:
                group.append(len(contexts))
                customer.append(index)
                month.append(t)
                product.append(_INDEX[pid])
                features.append(vector)
                rules.append(relevance_score(_BY_ID[pid], ctx))
                label.append(1 if pid in taken else 0)
            contexts.append(ctx)
    return Rows(
        np.array(group, dtype=np.int64),
        np.array(customer, dtype=np.int64),
        np.array(month, dtype=np.int64),
        np.array(product, dtype=np.int64),
        np.array(features, dtype=np.float64).reshape(-1, len(FEATURE_NAMES)),
        np.array(rules, dtype=np.float64),
        np.array(label, dtype=np.int64),
        tuple(contexts),
    )


@dataclass(frozen=True, slots=True)
class Split:
    train: Rows
    valid: Rows
    test: Rows
    train_last: int
    valid_months: tuple[int, int]
    test_first: int


def time_split(rows: Rows, train_last: int) -> Split:
    """Train on feature months ``<= train_last``; validate (model choice, early stopping) on
    ``train_last + 2`` and ``+ 3``; hold out ``>= train_last + 5`` as the test period. A row's label
    is the month after its feature month, so with a one-month gap before each later block no
    label of an earlier block falls in or after the next block's first feature month."""
    valid = (train_last + 2, train_last + 3)
    test_first = train_last + 5
    return Split(
        rows.take(rows.month <= train_last),
        rows.take((rows.month >= valid[0]) & (rows.month <= valid[1])),
        rows.take(rows.month >= test_first),
        train_last,
        valid,
        test_first,
    )
