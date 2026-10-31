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
    UNKNOWN_PAYEE_AGE,
    CurrentTransfer,
    PastTransfer,
    offline_features,
    online_features,
)

T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
DEV_A, DEV_B, DEV_C = uuid4(), uuid4(), uuid4()


def _past(
    minutes_ago: float, amount: int, direction: str = "DEBIT", device: UUID = DEV_A
) -> PastTransfer:
    return PastTransfer(T0 - timedelta(minutes=minutes_ago), direction, amount, device)  # type: ignore[arg-type]  # literal checked by the test data


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


def test_edge_cases_have_neutral_values() -> None:
    f = online_features(_now(1_00, payee_added_at=None, direction="CREDIT", device_id=None), [])
    assert f.ratio_to_avg_amount == 1.0
    assert f.payee_age_seconds == UNKNOWN_PAYEE_AGE
    assert f.velocity_10m == 0 and f.distinct_devices_7d == 0
    same_instant = online_features(_now(1_00), [_past(0, 5_00)])
    assert same_instant.velocity_10m == 0  # strictly before only


def _frame(transfers: tuple[Transfer, ...]) -> pd.DataFrame:
    rows = [asdict(t) for t in transfers]
    df = pd.DataFrame(rows)
    df["direction"] = df["direction"].astype(str)
    return df


def _online_all(transfers: tuple[Transfer, ...]) -> pd.DataFrame:
    by_customer: dict[UUID, list[Transfer]] = {}
    for t in transfers:
        by_customer.setdefault(t.customer_id, []).append(t)
    out = []
    for t in transfers:
        history = [
            PastTransfer(h.occurred_at, str(h.direction), h.amount_minor, h.device_id)  # type: ignore[arg-type]  # generator emits DEBIT/CREDIT
            for h in by_customer[t.customer_id]
            if h.id != t.id
        ]
        current = CurrentTransfer(
            t.occurred_at, str(t.direction), t.amount_minor, t.payee_added_at, t.device_id,  # type: ignore[arg-type]  # as above
            t.failed_logins_24h,
        )  # fmt: skip
        out.append(online_features(current, history).as_dict())
    return pd.DataFrame(out)


@pytest.mark.parametrize("seed", [7, 11])
def test_offline_and_online_features_are_identical(seed: int) -> None:
    data = generate_labelled(GeneratorConfig(seed=seed, customers=150, months=3))
    assert len(data.transfers) > 500
    offline = offline_features(_frame(data.transfers)).reset_index(drop=True)
    online = _online_all(data.transfers)
    assert list(offline.columns) == list(FEATURE_NAMES)
    for name in FEATURE_NAMES:
        left, right = offline[name].tolist(), online[name].tolist()
        mismatches = [i for i, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]
        assert mismatches == [], f"{name}: {len(mismatches)} rows differ, e.g. row {mismatches[:3]}"
    assert online["velocity_10m"].max() > 0 and (online["payee_age_seconds"] >= 0).any()
