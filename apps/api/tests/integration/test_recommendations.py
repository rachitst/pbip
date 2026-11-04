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
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.analytics import repository as analytics_repository
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanType
from pbip_api.modules.recommendations import service as recommendations_service
from pbip_ml.recommendations import CustomerRecommendationContext, Recommendation
from pbip_ml.recommendations.propensity import PropensityModel
from pbip_test_support import (
    Actor,
    age_payee,
    count_statements,
    month_start,
    post_deposit_at,
    post_spend_at,
)
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.security]
PERSONAL_LOAN = next(p for p in PRODUCTS if p.loan_type == LoanType.PERSONAL)


def test_recommendations_happy_path(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Customer receives top-3 personalized product recommendations with reasons."""
    alice = make_customer(salary=100_000_00)

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
    alice = make_customer(salary=100_000_00)
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

    def spy(
        ctx: CustomerRecommendationContext,
        top_k: int = 3,
        propensity: PropensityModel | None = None,
        apply_risk_band: bool = True,
    ) -> list[Recommendation]:
        seen.append(ctx)
        return real(ctx, top_k=top_k, propensity=propensity, apply_risk_band=apply_risk_band)

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


def _rich_history(
    client: TestClient,
    alice: Actor,
    bob: Actor,
    make_verified_payee: Callable[..., dict[str, Any]],
    engine: Engine,
) -> UUID:
    """History that tells the shared read apart from the separate ones (#343): salary and spends
    older than the 12-month health window (counted by the lifestyle features only), recurring
    salary inside it, a payment to another person and a move to a second own account (neither
    income nor spend). Returns the second account's id."""
    assert alice.account_id is not None
    now = utcnow()
    for months_back in (15, 14, 3, 2, 1):
        post_deposit_at(
            engine, alice.account_id, 90_000_00, "Salary", month_start(now, months_back)
        )
    post_spend_at(engine, alice.account_id, 7_000_00, "Restaurant dinner", month_start(now, 14))
    post_spend_at(engine, alice.account_id, 4_000_00, "Groceries", month_start(now, 2))
    post_spend_at(engine, alice.account_id, 2_500_00, "Movie tickets", month_start(now, 1))
    payee = make_verified_payee(alice, bob)
    age_payee(engine, payee["id"], 48)
    paid = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 12_345_00,
            "payee_id": payee["id"],
            "reference": "Dinner at a restaurant",
        },
    )
    assert paid.status_code == 201, paid.text
    opened = client.post(
        "/api/v1/accounts", headers=alice.headers, json={"account_type": "CURRENT"}
    )
    assert opened.status_code == 201, opened.text
    second = UUID(opened.json()["id"])
    moved = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 10_000_00,
            "to_account_id": str(second),
        },
    )
    assert moved.status_code == 201, moved.text
    return second


def test_one_read_of_the_customer_equals_the_separate_health_and_cash_flow_reads(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
) -> None:
    """#343: the profile takes the health window out of the lifestyle features' rows instead of
    reading it again. It is the same computation, not an approximation."""
    alice = make_customer(funds=200_000_00)
    second = _rich_history(client, alice, make_customer(), make_verified_payee, migrated_engine)
    assert alice.customer_id is not None and alice.account_id is not None
    now = utcnow()

    profile = analytics_service.get_customer_financial_profile(db, alice.customer_id, now)

    assert profile is not None
    assert [a.id for a in profile.accounts] == [alice.account_id, second]
    assert profile.health == analytics_service.get_financial_health_score(
        db, alice.customer_id, alice.account_id, now
    )
    assert profile.cash_flow == analytics_service.get_cash_flow_profile(
        db, alice.customer_id, alice.account_id, now
    )
    # The data exercises both sides of the window: health ignores the entries from 14-15 months
    # ago (12 months observed at most), the lifestyle share counts the old restaurant dinner.
    owned = analytics_repository.owned_account_ids(db, alice.customer_id)
    window = analytics_repository.get_financial_health_metrics(db, alice.account_id, owned, now)
    assert window.months_observed <= 12
    rfm = analytics_repository.extract_customer_rfm_features(db, alice.customer_id, now)
    assert rfm is not None and rfm.discretionary_ratio == profile.cash_flow.discretionary_ratio
    assert profile.cash_flow.discretionary_ratio > 0 and profile.cash_flow.monthly_spend_minor > 0
    assert analytics_service.get_customer_financial_profile(db, uuid.uuid4(), now) is None


def test_recommendations_equal_those_built_from_the_separate_reads(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    engine_inputs: list[CustomerRecommendationContext],
    db: Session,
) -> None:
    """#343 keeps the customer-visible result: the engine gets exactly the figures that the
    accounts, health-score and cash-flow services give separately, and so ranks the same."""
    alice = make_customer(funds=200_000_00)
    _rich_history(client, alice, make_customer(), make_verified_payee, migrated_engine)
    assert alice.customer_id is not None and alice.account_id is not None
    now = utcnow()

    served = recommendations_service.get_customer_recommendations(db, alice.customer_id, now)

    accounts = accounts_service.list_accounts(db, alice.customer_id)
    health = analytics_service.get_financial_health_score(
        db, alice.customer_id, accounts[0].id, now
    )
    cash = analytics_service.get_cash_flow_profile(db, alice.customer_id, accounts[0].id, now)
    [ctx] = engine_inputs
    assert (
        ctx.health_score,
        ctx.monthly_inflow_minor,
        ctx.monthly_spend_minor,
        ctx.savings_ratio,
        ctx.discretionary_ratio,
        ctx.buffer_months,
        ctx.emi_ratio,
        ctx.total_balance_minor,
    ) == (
        health.overall_score,
        cash.monthly_income_minor,
        cash.monthly_spend_minor,
        health.components.savings_rate.metric_value,
        cash.discretionary_ratio,
        health.components.buffer_months.metric_value,
        health.components.emi_to_income.metric_value,
        sum(account.available_balance for account in accounts),
    )
    assert served.recommendations  # ranked from that context by the unchanged engine


def test_recommendation_context_is_six_queries_and_the_request_seven(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    """#343 budget: 14 statements per context before #395, 10 after it, 6 now: accounts, one pass
    over the entries, segment, age band, loans, fixed deposits. Over HTTP the age band reads the
    customer that authentication has just loaded, so the request is 7: token check, customer, then
    the five. A new query here is a latency budget decision.

    ADR-0054: that is the cold build. While the accounts' row versions are unchanged, the entries,
    loans and deposits come from the cache: 3 for the service (accounts, segment, age band) and 4
    over HTTP (test_recommendation_context_cache.py has the invalidation cases)."""
    alice = make_customer(funds=200_000_00)
    assert alice.customer_id is not None
    with sessionmaker(bind=migrated_engine)() as session:
        recommendations_service.get_customer_recommendations(session, alice.customer_id)
        session.rollback()
        recommendations_service.clear_context_cache()
        with count_statements(migrated_engine) as service:
            recommendations_service.get_customer_recommendations(session, alice.customer_id)
        session.rollback()
        with count_statements(migrated_engine) as service_warm:
            recommendations_service.get_customer_recommendations(session, alice.customer_id)
    assert (len(service), len(service_warm)) == (6, 3), (service, service_warm)
    recommendations_service.clear_context_cache()
    with count_statements(migrated_engine) as http:
        assert client.get("/api/v1/recommendations", headers=alice.headers).status_code == 200
    with count_statements(migrated_engine) as http_warm:
        assert client.get("/api/v1/recommendations", headers=alice.headers).status_code == 200
    assert (len(http), len(http_warm)) == (7, 4), (http, http_warm)
    for sent in (http, http_warm):  # no second customer read
        assert not any("FROM customers" in sql for sql in sent[2:]), sent


def test_a_customer_with_a_loan_adds_only_the_instalments_query(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    engine_inputs: list[CustomerRecommendationContext],
) -> None:
    """The instalments are read only when there is a loan (an empty IN list was a round trip)."""
    alice = make_customer(salary=150_000_00)
    assert alice.customer_id is not None and alice.account_id is not None
    loan = client.post(
        "/api/v1/loans/apply",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "product_id": str(PERSONAL_LOAN.id),
            "amount_minor": 50_000_00,
            "tenure_months": 12,
            "account_id": str(alice.account_id),
        },
    )
    assert loan.status_code == 201, loan.text
    with sessionmaker(bind=migrated_engine)() as session:
        recommendations_service.get_customer_recommendations(session, alice.customer_id)
        session.rollback()
        recommendations_service.clear_context_cache()  # the cold build (ADR-0054)
        with count_statements(migrated_engine) as sent:
            recommendations_service.get_customer_recommendations(session, alice.customer_id)
    assert len(sent) == 7, sent
    kinds = [[held.kind.value for held in ctx.held_products] for ctx in engine_inputs]
    assert kinds == [["SAVINGS_ACCOUNT", "PERSONAL_LOAN"]] * 2
