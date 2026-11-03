"""Integration tests for customer personalized product recommendations API (TASK-054).

Verifies:
- GET /api/v1/recommendations returns top-3 eligible products ordered by the rule-based score
- Explanations and reasons are grounded and human-readable
- Authentication required (401 unauthenticated, 403 staff)
- Strict BOLA: Customer receives recommendations for their own profile only
- ADR-0015 / TASK-107: the engine gets only an age band, and only with consent
"""

import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.recommendations import service as recommendations_service
from pbip_ml.recommendations import CustomerRecommendationContext, Recommendation
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_recommendations_happy_path(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Customer receives top-3 personalized product recommendations with reasons."""
    alice = make_customer(funds=200_000_00)

    resp = client.get("/api/v1/recommendations", headers=alice.headers)

    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert data["customer_id"] == str(alice.customer_id)
    assert data["model_version"] == "rules-v1"
    assert "evaluated_at" in data

    recs = data["recommendations"]
    assert 1 <= len(recs) <= 3

    for item in recs:
        assert "product_id" in item
        assert item["product_type"] in ("SAVINGS", "LOAN", "INVESTMENT", "INSURANCE")
        assert item["title"]
        assert item["description"]
        assert 0.0 <= item["score"] <= 1.0
        assert len(item["reasons"]) >= 1
        assert item["cta_label"]
        assert "cta_path" not in item  # no product pages exist yet


def test_recommendations_unauthenticated_and_staff_forbidden(
    client: TestClient,
    make_staff: Callable[..., Actor],
) -> None:
    """Unauthenticated returns 401; staff returns 403."""
    # 401
    resp_unauth = client.get("/api/v1/recommendations")
    assert resp_unauth.status_code == 401

    # 403
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    resp_staff = client.get("/api/v1/recommendations", headers=analyst.headers)
    assert resp_staff.status_code == 403


def test_recommendations_use_the_customers_real_figures_and_ignore_date_of_birth_without_consent(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    """Reasons quote the real balance; a date of birth without consent changes nothing (ADR-0015)."""
    alice = make_customer(funds=200_000_00)
    before = client.get("/api/v1/recommendations", headers=alice.headers).json()
    reasons = [reason for item in before["recommendations"] for reason in item["reasons"]]
    assert reasons, before
    assert any("INR" in reason for reason in reasons)
    assert not any("%" in item["description"] for item in before["recommendations"])

    customer = db.get(Customer, alice.customer_id)
    assert customer is not None
    customer.date_of_birth = date(1950, 1, 1)  # 60+, but the customer has not consented
    db.commit()
    after = client.get("/api/v1/recommendations", headers=alice.headers).json()
    assert after["recommendations"] == before["recommendations"]


@pytest.fixture
def engine_inputs(monkeypatch: pytest.MonkeyPatch) -> list[CustomerRecommendationContext]:
    """Record exactly what the recommendation engine is given, then run it for real."""
    seen: list[CustomerRecommendationContext] = []
    real = recommendations_service.generate_recommendations

    def spy(ctx: CustomerRecommendationContext, top_k: int = 3) -> list[Recommendation]:
        seen.append(ctx)
        return real(ctx, top_k=top_k)

    monkeypatch.setattr(recommendations_service, "generate_recommendations", spy)
    return seen


def test_engine_gets_the_age_band_only_with_consent_and_never_the_date(
    client: TestClient,
    make_customer: Callable[..., Actor],
    engine_inputs: list[CustomerRecommendationContext],
    db: Session,
) -> None:
    alice = make_customer(funds=200_000_00)
    assert alice.customer_id is not None
    recs = "/api/v1/recommendations"
    dob = "/api/v1/customers/me/date-of-birth"
    consent = "/api/v1/customers/me/recommendations-consent"

    client.get(recs, headers=alice.headers)  # nothing provided
    client.put(dob, headers=alice.headers, json={"date_of_birth": "1950-01-01"})
    client.get(recs, headers=alice.headers)  # date but no consent
    client.put(consent, headers=alice.headers, json={"granted": True})
    granted = client.get(recs, headers=alice.headers).json()  # date and consent
    # every eligible product, as the RM insights view asks for it: same consented band
    everything = recommendations_service.get_customer_recommendations(
        db, alice.customer_id, top_k=20
    )
    client.put(consent, headers=alice.headers, json={"granted": False})
    client.get(recs, headers=alice.headers)  # withdrawn: stops at once
    client.put(consent, headers=alice.headers, json={"granted": True})
    client.delete(dob, headers=alice.headers)
    client.get(recs, headers=alice.headers)  # consent but the date was deleted

    assert [ctx.age_band for ctx in engine_inputs] == [
        "unknown",
        "unknown",
        "60+",
        "60+",
        "unknown",
        "unknown",
    ]
    for ctx in engine_inputs:
        dumped = ctx.model_dump()
        assert not {key for key in dumped if "birth" in key or "dob" in key}
        assert "1950" not in str(dumped)
    # the engine's output (customer and RM views) never carries the date either
    assert "1950" not in str(granted) and "1950" not in everything.model_dump_json()


def test_recommendations_are_scoped_to_the_authenticated_customer(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=200_000_00)
    bob = make_customer()
    a = client.get("/api/v1/recommendations", headers=alice.headers).json()
    b = client.get("/api/v1/recommendations", headers=bob.headers).json()
    assert a["customer_id"] == str(alice.customer_id)
    assert b["customer_id"] == str(bob.customer_id)
    # a client-supplied customer id is not a parameter: it is ignored
    c = client.get(
        f"/api/v1/recommendations?customer_id={bob.customer_id}", headers=alice.headers
    ).json()
    assert c["customer_id"] == str(alice.customer_id)


def _statements(engine: Engine, call: Callable[[], object]) -> int:
    count = 0

    def counter(*_args: object) -> None:
        nonlocal count
        count += 1

    event.listen(engine, "before_cursor_execute", counter)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", counter)
    return count


def test_context_reads_the_health_window_once(
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    """#343: the health score and the cash-flow profile share one ownership check, one read of the
    account ids and one pass over the entries. It was 14 statements (each read twice); now 10:
    accounts, ownership, account ids, health entries, lifestyle entries, segment, age band, loans,
    loan instalments, fixed deposits. A new query here is a latency budget decision."""
    alice = make_customer(funds=200_000_00)
    with sessionmaker(bind=migrated_engine)() as session:
        assert alice.customer_id is not None
        recommendations_service.get_customer_recommendations(session, alice.customer_id)
        session.rollback()
        count = _statements(
            migrated_engine,
            lambda: recommendations_service.get_customer_recommendations(
                session, alice.customer_id
            ),
        )
    assert count == 10


def test_shared_health_and_cash_flow_equal_the_separate_calls(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
) -> None:
    """The combined read is the same computation, not an approximation."""
    alice = make_customer(funds=200_000_00)
    bob = make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    resp = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 12_345_00,
            "payee_id": payee["id"],
            "reference": "Dinner at a restaurant",
        },
    )
    assert resp.status_code == 201, resp.text
    assert alice.customer_id is not None and alice.account_id is not None
    now = utcnow()
    health, cash = analytics_service.get_health_and_cash_flow(
        db, alice.customer_id, alice.account_id, now
    )
    assert health == analytics_service.get_financial_health_score(
        db, alice.customer_id, alice.account_id, now
    )
    assert cash == analytics_service.get_cash_flow_profile(
        db, alice.customer_id, alice.account_id, now
    )
    assert cash.monthly_spend_minor > 0
