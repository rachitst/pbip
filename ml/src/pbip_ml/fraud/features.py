"""Fraud features with offline/online parity (TASK-024).

Two independent implementations of the same definitions:

* :func:`online_features` - one transfer at decision time, from the customer's prior events (what
  the API can fetch for the transfer being scored). Pure Python, no pandas, fast.
* :func:`offline_features` - every transfer of a labelled dataset at once, vectorised with pandas
  (what model training uses).

``tests/test_fraud_features.py`` proves they agree EXACTLY (zero tolerance) on generated data, so a
model trained offline sees the same numbers it is served online.

Point-in-time rule: a transfer's features use only events strictly BEFORE it (``occurred_at`` <
the transfer's), except ``distinct_devices_7d``, which counts the current device too (the device in
use is known at decision time). Money is integer paise; ratios are exact floats of integer sums.

Definitions (all windows end at the transfer, exclusive):

==========================  ===========================================================
ratio_to_avg_amount         amount / mean prior DEBIT amount in 30 days; 1.0 with no history
payee_age_seconds           seconds since the payee was added; -1 for credits / unknown
velocity_10m                prior DEBITs in the last 10 minutes
failure_count_24h           failed logins in the last 24 hours (counted by the platform)
distinct_devices_7d         distinct devices on the customer's transfers in 7 days, incl. this one
==========================  ===========================================================
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

import numpy as np
import pandas as pd

FEATURE_NAMES = (
    "ratio_to_avg_amount",
    "payee_age_seconds",
    "velocity_10m",
    "failure_count_24h",
    "distinct_devices_7d",
)
AVG_WINDOW = timedelta(days=30)
VELOCITY_WINDOW = timedelta(minutes=10)
DEVICE_WINDOW = timedelta(days=7)
UNKNOWN_PAYEE_AGE = -1


@dataclass(frozen=True, slots=True)
class PastTransfer:
    occurred_at: datetime
    direction: Literal["DEBIT", "CREDIT"]
    amount_minor: int
    device_id: UUID | None


@dataclass(frozen=True, slots=True)
class CurrentTransfer:
    occurred_at: datetime
    direction: Literal["DEBIT", "CREDIT"]
    amount_minor: int
    payee_added_at: datetime | None
    device_id: UUID | None
    failed_logins_24h: int


@dataclass(frozen=True, slots=True)
class FraudFeatures:
    ratio_to_avg_amount: float
    payee_age_seconds: int
    velocity_10m: int
    failure_count_24h: int
    distinct_devices_7d: int

    def as_dict(self) -> dict[str, float | int]:
        return {name: getattr(self, name) for name in FEATURE_NAMES}


def online_features(current: CurrentTransfer, history: Sequence[PastTransfer]) -> FraudFeatures:
    """Features for one transfer from the customer's earlier transfers (any order)."""
    t = current.occurred_at
    prior = [h for h in history if h.occurred_at < t]
    debits_30d = [
        h.amount_minor for h in prior if h.direction == "DEBIT" and h.occurred_at >= t - AVG_WINDOW
    ]
    ratio = current.amount_minor * len(debits_30d) / sum(debits_30d) if debits_30d else 1.0
    if current.direction == "DEBIT" and current.payee_added_at is not None:
        payee_age = int((t - current.payee_added_at).total_seconds())
    else:
        payee_age = UNKNOWN_PAYEE_AGE
    velocity = sum(
        1 for h in prior if h.direction == "DEBIT" and h.occurred_at >= t - VELOCITY_WINDOW
    )
    devices = {h.device_id for h in prior if h.occurred_at >= t - DEVICE_WINDOW and h.device_id}
    if current.device_id is not None:
        devices.add(current.device_id)
    return FraudFeatures(
        ratio_to_avg_amount=float(ratio),
        payee_age_seconds=payee_age,
        velocity_10m=velocity,
        failure_count_24h=current.failed_logins_24h,
        distinct_devices_7d=len(devices),
    )


def _window_count_sum(
    times: np.ndarray, mask: np.ndarray, amounts: np.ndarray, window_ns: int
) -> tuple[np.ndarray, np.ndarray]:
    """Per row i: count and sum of masked rows with t_i - window <= t_j < t_i (times sorted)."""
    masked_amounts = np.where(mask, amounts, 0)
    cum_count = np.concatenate([[0], np.cumsum(mask.astype(np.int64))])
    cum_sum = np.concatenate([[0], np.cumsum(masked_amounts.astype(np.int64))])
    end = np.searchsorted(times, times, side="left")  # first index with t_j >= t_i
    start = np.searchsorted(times, times - window_ns, side="left")  # first t_j >= t_i - window
    return cum_count[end] - cum_count[start], cum_sum[end] - cum_sum[start]


def _customer_block(group: pd.DataFrame) -> pd.DataFrame:
    group = group.sort_values("occurred_at", kind="stable")
    times = group["occurred_at"].to_numpy(dtype="datetime64[ns]").astype(np.int64)
    is_debit = (group["direction"] == "DEBIT").to_numpy()
    amounts = group["amount_minor"].to_numpy(dtype=np.int64)

    count_30d, sum_30d = _window_count_sum(times, is_debit, amounts, _ns(AVG_WINDOW))
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(count_30d > 0, amounts * count_30d / np.maximum(sum_30d, 1), 1.0)
    velocity, _ = _window_count_sum(times, is_debit, amounts, _ns(VELOCITY_WINDOW))

    devices = group["device_id"].to_numpy()
    start = np.searchsorted(times, times - _ns(DEVICE_WINDOW), side="left")
    end = np.searchsorted(times, times, side="left")
    distinct = np.array(
        [
            len({d for d in devices[s:e] if d is not None and d == d} | ({cur} if cur else set()))
            for s, e, cur in zip(start, end, devices, strict=True)
        ],
        dtype=np.int64,
    )
    return pd.DataFrame(
        {
            "ratio_to_avg_amount": ratio.astype(float),
            "velocity_10m": velocity.astype(np.int64),
            "distinct_devices_7d": distinct,
        },
        index=group.index,
    )


def _ns(delta: timedelta) -> int:
    return int(delta.total_seconds()) * 1_000_000_000


def offline_features(transfers: pd.DataFrame) -> pd.DataFrame:
    """Vectorised features for every row of a transfers table (training path).

    Expects columns: customer_id, occurred_at (tz-aware), direction, amount_minor, payee_added_at,
    device_id, failed_logins_24h. Returns one row per input row, same index, FEATURE_NAMES columns.
    """
    df = transfers.copy()
    df["occurred_at"] = pd.to_datetime(df["occurred_at"], utc=True)
    df["payee_added_at"] = pd.to_datetime(df["payee_added_at"], utc=True)
    parts = [_customer_block(group) for _, group in df.groupby("customer_id", sort=False)]
    windowed = pd.concat(parts).reindex(df.index)
    has_payee = (df["direction"] == "DEBIT") & df["payee_added_at"].notna()
    age = (df["occurred_at"] - df["payee_added_at"]).dt.total_seconds()
    payee_age = np.where(has_payee, np.trunc(age.fillna(0)), UNKNOWN_PAYEE_AGE).astype(np.int64)
    return pd.DataFrame(
        {
            "ratio_to_avg_amount": windowed["ratio_to_avg_amount"],
            "payee_age_seconds": payee_age,
            "velocity_10m": windowed["velocity_10m"],
            "failure_count_24h": df["failed_logins_24h"].astype(np.int64),
            "distinct_devices_7d": windowed["distinct_devices_7d"],
        },
        index=df.index,
    )
