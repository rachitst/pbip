"""Customer behavioural segmentation (TASK-044): features from the ledger, the offline job, the
read-only endpoint. No training runs on the request path; the endpoint is 404 until the job ran."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.analytics import repository
from pbip_api.modules.analytics.models import CustomerSegment
from pbip_api.modules.analytics.segmentation_job import run_offline_segmentation_job
from pbip_ml.segmentation.contract import CustomerSegmentName
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]

SEGMENT_URL = "/api/v1/analytics/segment"


def _transfer(client: TestClient, actor: Actor, payee_id: str, amount: int, reference: str) -> None:
    import uuid

    resp = client.post(
        "/api/v1/transfers",
        headers={**actor.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(actor.account_id),
            "amount_minor": amount,
            "payee_id": payee_id,
            "reference": reference,
        },
    )
    assert resp.status_code == 201, resp.text


def test_rfm_features_come_from_posted_ledger_entries(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
) -> None:
    alice = make_customer(funds=100_000)
    bob = make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    _transfer(client, alice, payee["id"], 20_000, "Swiggy dinner order")  # dining: discretionary
    _transfer(client, alice, payee["id"], 30_000, "October rent")

    rfm = repository.extract_customer_rfm_features(db, alice.customer_id, utcnow())
    assert rfm is not None
    assert rfm.recency_days == 0 and rfm.frequency == 2.0  # two debits within the first month
    assert rfm.monetary_minor == 50_000
    assert rfm.savings_ratio == 0.5  # (100,000 - 50,000) / 100,000
    assert rfm.discretionary_ratio == 0.4  # 20,000 / 50,000

    # A customer without any ledger activity has no features (nothing to cluster).
    idle = make_customer()
    assert repository.extract_customer_rfm_features(db, idle.customer_id, utcnow()) is None


def test_segment_endpoint_is_404_until_the_job_assigned_one(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=10_000)
    resp = client.get(SEGMENT_URL, headers=alice.headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "not_found"


def test_job_skips_cleanly_when_too_few_customers_have_activity(
    make_customer: Callable[..., Actor], db: Session
) -> None:
    make_customer()  # registered, no ledger activity
    result = run_offline_segmentation_job(db, utcnow())
    assert result.n_clusters == 0 and result.total_customers_assigned == 0
    assert result.skipped_reason is not None and "at least 2" in result.skipped_reason
    assert db.execute(select(func.count()).select_from(CustomerSegment)).scalar_one() == 0


def test_job_persists_explainable_segments_idempotently_and_the_endpoint_serves_them(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
) -> None:
    spenders = [make_customer(funds=500_000) for _ in range(3)]
    saver = make_customer(funds=500_000)
    sink = make_customer()
    payees = {actor.customer_id: make_verified_payee(actor, sink) for actor in (*spenders, saver)}
    for payee in payees.values():
        age_payee(migrated_engine, payee["id"], 48)
    for actor in (*spenders, saver):
        _transfer(client, actor, payees[actor.customer_id]["id"], 5_000, "Groceries at BigBasket")
    for actor in spenders:  # kept small and few: large or rapid transfers go to fraud review
        for _ in range(2):
            _transfer(
                client, actor, payees[actor.customer_id]["id"], 15_000, "Zomato dinner, Amazon"
            )
    idle = make_customer()  # no activity: must not get a segment

    first = run_offline_segmentation_job(db, utcnow(), n_clusters=2)
    assert first.skipped_reason is None and first.n_clusters == 2
    assert first.n_samples == 5  # four customers with debits plus the sink with credits
    assert first.total_customers_assigned == 5
    assert sum(first.cluster_distribution.values()) == 5
    assert first.silhouette_score is not None and first.davies_bouldin_index is not None
    rows = db.execute(select(CustomerSegment)).scalars().all()
    assert {r.customer_id for r in rows} == {a.customer_id for a in (*spenders, saver, sink)}
    assert all(r.segment in CustomerSegmentName.__members__ for r in rows)

    # The read-only endpoint serves exactly the stored row of the principal's own customer.
    me = client.get(SEGMENT_URL, headers=spenders[0].headers)
    assert me.status_code == 200, me.text
    body = me.json()
    stored = next(r for r in rows if r.customer_id == spenders[0].customer_id)
    assert body["customer_id"] == str(spenders[0].customer_id)
    assert body["segment"] == stored.segment and body["explanation"] == stored.explanation
    assert body["monetary_minor"] == stored.monetary_minor
    assert body["discretionary_ratio"] == stored.discretionary_ratio
    assert client.get(SEGMENT_URL, headers=idle.headers).status_code == 404

    # Re-running updates in place: same customers, no duplicate rows.
    second = run_offline_segmentation_job(db, utcnow(), n_clusters=2)
    assert second.total_customers_assigned == 5
    assert db.execute(select(func.count()).select_from(CustomerSegment)).scalar_one() == 5
