"""Feature contract draft + offline reference implementation (TASK-010)."""

import random
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pbip_ml.features.contract import (
    FEATURE_CATALOGUE,
    FEATURE_SET_VERSION,
    CustomerFeatures,
    TransactionEvent,
)
from pbip_ml.features.offline import compute_customer_features, point_in_time
from pydantic import ValidationError

pytestmark = pytest.mark.unit

ALICE, BOB = uuid.UUID(int=1), uuid.UUID(int=2)
AS_OF = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)


def event(
    minutes_before: float, amount: int = 1000, direction: str = "DEBIT", **kw: object
) -> TransactionEvent:
    return TransactionEvent.model_validate(
        {
            "customer_id": kw.get("customer_id", ALICE),
            "occurred_at": AS_OF - timedelta(minutes=minutes_before),
            "direction": direction,
            "amount_minor": amount,
            "merchant": kw.get("merchant", "FreshCart"),
        }
    )


# --------------------------------------------------------------------- contract


def test_catalogue_documents_exactly_the_feature_fields() -> None:
    meta = {"feature_set_version", "customer_id", "as_of"}
    fields = set(CustomerFeatures.model_fields) - meta
    assert {d.name for d in FEATURE_CATALOGUE} == fields
    assert {"avg_amount_30d", "transfer_velocity_10m"} <= fields


def test_input_events_cannot_carry_labels_or_naive_times() -> None:
    good = event(5).model_dump()
    with pytest.raises(ValidationError, match="Extra inputs"):
        TransactionEvent.model_validate({**good, "is_fraud": True})
    with pytest.raises(ValidationError, match="timezone-aware"):
        TransactionEvent.model_validate({**good, "occurred_at": datetime(2025, 1, 1)})  # noqa: DTZ001
    with pytest.raises(ValidationError):
        TransactionEvent.model_validate({**good, "amount_minor": 0})


def test_feature_vectors_are_strict_and_versioned() -> None:
    features = compute_customer_features(ALICE, [], AS_OF)
    assert features.feature_set_version == FEATURE_SET_VERSION
    with pytest.raises(ValidationError):
        CustomerFeatures.model_validate({**features.model_dump(), "label": 1})
    with pytest.raises(ValidationError):
        CustomerFeatures.model_validate({**features.model_dump(), "avg_amount_30d": -1})


# --------------------------------------------------------------------- features


def test_known_history_produces_expected_features() -> None:
    history = [
        event(3, 500, merchant="CityCab"),
        event(8, 1500),
        event(60 * 3, 2500, merchant="CineMax"),
        event(60 * 24 * 10, 4000),
        event(60 * 24 * 40, 99_999),  # outside the 30-day window
        event(60 * 24 * 2, 200_000, direction="CREDIT", merchant="Employer Payroll"),
        event(1, 7777, customer_id=BOB),  # someone else's activity
    ]
    f = compute_customer_features(ALICE, history, AS_OF)
    assert f.debit_count_30d == 4
    assert f.debit_total_30d == 8500 and f.avg_amount_30d == 2125 and f.max_amount_30d == 4000
    assert f.credit_total_30d == 200_000
    assert f.transfer_velocity_10m == 2 and f.transfer_velocity_24h == 3
    assert f.distinct_merchants_30d == 3
    assert f.days_since_last_txn == pytest.approx(3 / 1440)


def test_night_ratio_uses_indian_local_time() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    as_of = datetime(2025, 6, 15, 12, 0, tzinfo=ist)
    night = TransactionEvent(
        customer_id=ALICE, occurred_at=datetime(2025, 6, 15, 2, 30, tzinfo=ist),
        direction="DEBIT", amount_minor=100, merchant="X",
    )  # fmt: skip
    day = night.model_copy(update={"occurred_at": datetime(2025, 6, 15, 9, 0, tzinfo=ist)})
    assert compute_customer_features(ALICE, [night, day], as_of).night_debit_ratio_30d == 0.5


def test_no_history_gives_zeros_and_unknown_recency() -> None:
    f = compute_customer_features(ALICE, [], AS_OF)
    assert (f.avg_amount_30d, f.debit_count_30d, f.night_debit_ratio_30d) == (0, 0, 0.0)
    assert f.days_since_last_txn is None


def test_an_event_exactly_at_as_of_is_excluded() -> None:
    assert point_in_time([event(0)], AS_OF) == []
    assert compute_customer_features(ALICE, [event(0, 5000)], AS_OF).debit_count_30d == 0


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_customer_features(ALICE, [], datetime(2025, 1, 1))  # noqa: DTZ001


# ------------------------------------------------------------ leakage property

event_strategy = st.builds(
    lambda minutes, amount, debit: event(minutes, amount, "DEBIT" if debit else "CREDIT"),
    st.floats(min_value=-60 * 24 * 60, max_value=60 * 24 * 60, allow_nan=False),
    st.integers(min_value=1, max_value=10_000_000),
    st.booleans(),
)


@settings(max_examples=200)
@given(history=st.lists(event_strategy, max_size=30), future=st.lists(event_strategy, max_size=30))
def test_features_never_depend_on_events_at_or_after_as_of(
    history: list[TransactionEvent], future: list[TransactionEvent]
) -> None:
    """Target leakage guard: anything observed from as_of onwards cannot change the features."""
    past = point_in_time(history, AS_OF)
    later = [e for e in future if e.occurred_at >= AS_OF]
    shuffled = past + later
    random.Random(0).shuffle(shuffled)
    assert compute_customer_features(ALICE, shuffled, AS_OF) == compute_customer_features(
        ALICE, past, AS_OF
    )
