"""TASK-024: fraud features, and EXACT parity between the offline (pandas) and online paths."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pandas as pd
import pytest
from pbip_datagen.fraud import Transfer, generate_labelled
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud.features import (
    FEATURE_NAMES,
    NO_ACTIVITY,
    UNKNOWN_PAYEE_AGE,
    CurrentTransfer,
    PastTransfer,
    offline_features,
    online_features,
)

T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
DEV_A, DEV_B, DEV_C = uuid4(), uuid4(), uuid4()


def _past(
    minutes_ago: float,
    amount: int,
    direction: str = "DEBIT",
    device: UUID | None = DEV_A,
    activity: bool = True,
) -> PastTransfer:
    return PastTransfer(T0 - timedelta(minutes=minutes_ago), direction, amount, device, activity)  # type: ignore[arg-type]  # literal checked by the test data


def _now(amount: int, **kw: object) -> CurrentTransfer:
    base = {
        "occurred_at": T0,
        "direction": "DEBIT",
        "amount_minor": amount,
        "payee_added_at": T0 - timedelta(minutes=3),
        "device_id": DEV_A,
        "failed_logins_24h": 0,
    }
    base.update(kw)
    return CurrentTransfer(**base)  # type: ignore[arg-type]  # test helper  # fmt: skip


def test_transaction_and_account_features_by_definition() -> None:
    history = [
        _past(60 * 24 * 40, 1_000_00),  # outside 30 days: ignored for the average
        _past(60 * 24 * 2, 2_000_00, device=DEV_B),
        _past(60 * 24 * 1, 4_000_00, device=DEV_C),
        _past(5, 3_000_00),  # inside 10 minutes
        _past(9, 500_00, direction="CREDIT"),  # credits are not velocity or average
        _past(-1, 9_999_00),  # in the future: never used (point in time)
    ]
    f = online_features(_now(9_000_00, failed_logins_24h=4), history)
    assert f.ratio_to_avg_amount == 9_000_00 * 3 / (2_000_00 + 4_000_00 + 3_000_00)
    assert f.payee_age_seconds == 180  # "payee added 3m ago"
    assert f.velocity_10m == 1
    assert f.failure_count_24h == 4
    assert f.distinct_devices_7d == 3  # A (current + 5 min ago), B, C


DAY_MIN = 24 * 60


def test_structuring_window_counts_near_threshold_movements_either_way() -> None:
    """#274 F7 (ADR-0051): prior activity in 72 h, each 90% to 99.99% of INR 1,00,000."""
    history = [
        _past(10, 95_000_00, "CREDIT", None),
        _past(DAY_MIN, 90_000_00, "CREDIT", None),  # exactly 90%: counts
        _past(2 * DAY_MIN, 99_999_99),  # a debit just under: counts
        _past(72 * 60 - 1, 99_000_00, "CREDIT", None),  # just inside 72 h
        _past(72 * 60 + 1, 99_000_00, "CREDIT", None),  # just outside
        _past(30, 89_999_99, "CREDIT", None),  # under the band
        _past(40, 1_00_000_00, "CREDIT", None),  # the threshold itself is not "under" it
        _past(50, 95_000_00, "CREDIT", None, activity=False),  # not customer activity
        _past(0, 95_000_00, "CREDIT", None),  # same instant: not before
    ]
    assert online_features(_now(95_000_00), history).near_threshold_72h == 4


def test_days_since_last_activity_ignores_bank_postings() -> None:
    """#275 F8 (ADR-0051): whole days since the last customer activity; fees do not count."""
    history = [
        _past(200 * DAY_MIN + 5, 2_000_00),
        _past(3 * DAY_MIN, 100_00, activity=False),  # a fee: the account stays dormant
        _past(-DAY_MIN, 5_00),  # in the future
    ]
    assert online_features(_now(2_00_000_00), history).days_since_last_activity == 200
    credit = [_past(DAY_MIN - 1, 1_000_00, "CREDIT", None)]  # money received is activity too
    assert online_features(_now(1_00), credit).days_since_last_activity == 0
    assert online_features(_now(1_00), []).days_since_last_activity == NO_ACTIVITY
    fees_only = [_past(DAY_MIN, 100_00, activity=False)]
    assert online_features(_now(1_00), fees_only).days_since_last_activity == NO_ACTIVITY


def test_devices_are_counted_on_debits_only() -> None:
    """ADR-0051: a credit's device is the sender's business; the API has none for it."""
    history = [_past(60, 1_000_00, "CREDIT", DEV_B), _past(70, 1_000_00, device=DEV_C)]
    assert online_features(_now(1_00), history).distinct_devices_7d == 2  # A (current), C


def test_edge_cases_have_neutral_values() -> None:
    f = online_features(_now(1_00, payee_added_at=None, direction="CREDIT", device_id=None), [])
    assert f.ratio_to_avg_amount == 1.0
    assert f.payee_age_seconds == UNKNOWN_PAYEE_AGE
    assert f.velocity_10m == 0 and f.distinct_devices_7d == 0
    same_instant = online_features(_now(1_00), [_past(0, 5_00)])
    assert same_instant.velocity_10m == 0  # strictly before only


def _activity(t: Transfer) -> bool:
    """A deterministic share of rows marked as bank postings, so the flag's handling is compared
    too (generated rows are all customer activity)."""
    return t.amount_minor % 7 != 0


def _frame(transfers: tuple[Transfer, ...], with_activity: bool = False) -> pd.DataFrame:
    rows = [asdict(t) for t in transfers]
    df = pd.DataFrame(rows)
    df["direction"] = df["direction"].astype(str)
    if with_activity:
        df["activity"] = [_activity(t) for t in transfers]
    return df


def _online_all(transfers: tuple[Transfer, ...], with_activity: bool = False) -> pd.DataFrame:
    by_customer: dict[UUID, list[Transfer]] = {}
    for t in transfers:
        by_customer.setdefault(t.customer_id, []).append(t)
    out = []
    for t in transfers:
        history = [
            PastTransfer(
                h.occurred_at,
                str(h.direction),  # type: ignore[arg-type]  # generator emits DEBIT/CREDIT
                h.amount_minor,
                h.device_id,
                _activity(h) if with_activity else True,
            )
            for h in by_customer[t.customer_id]
            if h.id != t.id
        ]
        current = CurrentTransfer(
            t.occurred_at, str(t.direction), t.amount_minor, t.payee_added_at, t.device_id,  # type: ignore[arg-type]  # as above
            t.failed_logins_24h,
        )  # fmt: skip
        out.append(online_features(current, history).as_dict())
    return pd.DataFrame(out)


@pytest.mark.parametrize(
    ("seed", "months", "with_activity"), [(7, 3, False), (11, 3, True), (13, 9, False)]
)
def test_offline_and_online_features_are_identical(
    seed: int, months: int, with_activity: bool
) -> None:
    """9 months: long enough for F8 (dormancy) rows and their 180+ day gaps."""
    data = generate_labelled(GeneratorConfig(seed=seed, customers=150, months=months))
    assert len(data.transfers) > 500
    offline = offline_features(_frame(data.transfers, with_activity)).reset_index(drop=True)
    online = _online_all(data.transfers, with_activity)
    assert list(offline.columns) == list(FEATURE_NAMES)
    for name in FEATURE_NAMES:
        left, right = offline[name].tolist(), online[name].tolist()
        mismatches = [i for i, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]
        assert mismatches == [], f"{name}: {len(mismatches)} rows differ, e.g. row {mismatches[:3]}"
    assert online["velocity_10m"].max() > 0 and (online["payee_age_seconds"] >= 0).any()
    assert online["near_threshold_72h"].max() >= 2  # F7 deposits in the data
    assert (online["days_since_last_activity"] == NO_ACTIVITY).any()
    assert online["days_since_last_activity"].max() >= (30 if months == 3 else 180)
