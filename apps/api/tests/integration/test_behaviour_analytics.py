"""TASK-101 (UC-12 Behavioural Analytics): segment mix, spending, product affinity, engagement.

Known inputs go through the real API (transfers, an account opening, a loan, clickstream batches);
only the weekly segment assignment is written directly, as the offline job would (ADR-0012). Every
assertion is an exact number worked out by hand from those inputs.
"""

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.fraud.dependencies import get_fraud_scorer
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanType
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_ml.recommendations.engine import FIXED_DEPOSIT_ID, SAVINGS_ACCOUNT_ID
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration

URL = "/api/v1/insights/behaviour"
PERSONAL = next(p for p in PRODUCTS if p.loan_type == LoanType.PERSONAL)


def assign_segment(engine: Engine, customer: Actor, segment: str, monetary: int) -> None:
    """What the weekly K-Means job writes (ADR-0012); the request path never assigns segments."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customer_segments (id, customer_id, segment, confidence_score, "
                "recency_days, frequency, monetary_minor, savings_ratio, discretionary_ratio, "
                "explanation, created_at, updated_at) VALUES (gen_random_uuid(), :c, :s, 0.9, "
                ":r, 4.0, :m, 0.25, 0.5, 'test', now(), now())"
            ),
            {"c": customer.customer_id, "s": segment, "m": monetary, "r": monetary // 1000},
        )


def transfer(client: TestClient, sender: Actor, payee: dict[str, Any], amount: int) -> None:
    r = client.post(
        "/api/v1/transfers",
        headers={**sender.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={"from_account_id": str(sender.account_id), "amount_minor": amount,
              "payee_id": payee["id"], "reference": "dinner"},
    )  # fmt: skip
    assert r.status_code == 201 and r.json()["status"] == "POSTED", r.text


def events(client: TestClient, actor: Actor, batch: list[tuple[str, datetime, str, dict[str, str]]]) -> None:  # fmt: skip
    payload = {
        "events": [
            {"event_type": t, "event_timestamp": at.isoformat(), "session_id": s,
             "page_url": "/insights", "properties": props}
            for t, at, s, props in batch
        ]
    }  # fmt: skip
    r = client.post("/api/v1/events/batch", headers=actor.headers, json=payload)
    assert r.status_code == 202 and r.json()["accepted"] == len(batch), r.text


@pytest.fixture
def population(
    app: FastAPI,
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> dict[str, Any]:
    """A, B: HIGH_NET_WORTH; C: BUDGET_CONSCIOUS; D: never segmented."""
    app.dependency_overrides[get_fraud_scorer] = lambda: FixedDecisionScorer(FraudDecision.ALLOW)
    a = make_customer(funds=1_00_000_00)
    b = make_customer(salary=50_000_00)  # recurring income for the loan (ADR-0043); 1 lakh balance
    c, d = make_customer(), make_customer()
    assign_segment(migrated_engine, a, "HIGH_NET_WORTH", 30_000_00)
    assign_segment(migrated_engine, b, "HIGH_NET_WORTH", 50_000_00)
    assign_segment(migrated_engine, c, "BUDGET_CONSCIOUS", 8_000_00)

    to_c = make_verified_payee(a, c)
    to_d = make_verified_payee(b, d)
    opened = client.post("/api/v1/accounts", headers=a.headers, json={"account_type": "CURRENT"})
    assert opened.status_code == 201, opened.text
    a_current = replace(a, account_number=opened.json()["account_number"])
    to_self = make_verified_payee(a, a_current)
    for payee in (to_c, to_d, to_self):
        age_payee(migrated_engine, payee["id"], 48)
    transfer(client, a, to_c, 10_000)
    transfer(client, a, to_c, 5_000)
    transfer(client, a, to_self, 40_000)  # own money moving: not spend
    transfer(client, b, to_d, 7_000)
    loan = client.post(
        "/api/v1/loans/apply",
        headers={**b.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={"product_id": str(PERSONAL.id), "amount_minor": 1_00_000_00, "tenure_months": 12,
              "account_id": str(b.account_id)},
    )  # fmt: skip
    assert loan.status_code == 201, loan.text
    app.dependency_overrides.pop(get_fraud_scorer)

    base = (datetime.now(UTC) - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)  # fmt: skip
    click = "product_clicked"
    events(client, a, [
        ("page_view", base, "s-a1", {}),
        (click, base + timedelta(seconds=60), "s-a1", {"product_id": str(SAVINGS_ACCOUNT_ID)}),
        ("account_viewed", base + timedelta(seconds=120), "s-a1", {}),
        ("page_view", base - timedelta(days=40), "s-a-old", {}),
    ])  # fmt: skip
    events(client, c, [
        (click, base, "s-c1", {"product_id": str(FIXED_DEPOSIT_ID)}),
        ("page_view", base + timedelta(seconds=30), "s-c1", {}),
    ])  # fmt: skip
    events(client, d, [(click, base + timedelta(hours=1), "s-d1", {"product_id": "not-in-catalogue"})])  # fmt: skip
    return {"a": a, "b": b, "c": c, "d": d, "day": base.date().isoformat()}


def get(client: TestClient, actor: Actor, query: str = "") -> dict[str, Any]:
    r = client.get(f"{URL}{query}", headers=actor.headers)
    assert r.status_code == 200, r.text
    return dict(r.json())


def test_segment_distribution_and_profiles_are_exact(
    client: TestClient, make_staff: Callable[..., Actor], population: dict[str, Any]
) -> None:
    body = get(client, make_staff(RoleName.RELATIONSHIP_MANAGER))
    seg = body["segments"]
    assert seg["total_customers"] == 4 and seg["last_segmented_at"] is not None
    by = {i["segment"]: i for i in seg["items"]}
    assert list(by) == ["HIGH_NET_WORTH", "BUDGET_CONSCIOUS", "FREQUENT_TRANSACTOR",
                        "DISCRETIONARY_SPENDER", "INACTIVE_SAVER", "UNSEGMENTED"]  # fmt: skip
    assert (by["HIGH_NET_WORTH"]["customers"], by["HIGH_NET_WORTH"]["share_pct"]) == (2, 50.0)
    assert (by["BUDGET_CONSCIOUS"]["customers"], by["BUDGET_CONSCIOUS"]["share_pct"]) == (1, 25.0)
    assert (by["UNSEGMENTED"]["customers"], by["UNSEGMENTED"]["share_pct"]) == (1, 25.0)
    assert by["INACTIVE_SAVER"] == {
        "segment": "INACTIVE_SAVER", "customers": 0, "share_pct": 0.0, "avg_recency_days": None,
        "avg_monthly_transactions": None, "avg_monthly_spend_minor": None,
        "avg_savings_ratio": None, "avg_discretionary_ratio": None,
    }  # fmt: skip
    hnw = by["HIGH_NET_WORTH"]
    assert hnw["avg_monthly_spend_minor"] == 40_000_00  # (30,000 + 50,000) / 2
    assert hnw["avg_recency_days"] == 4000.0 and hnw["avg_monthly_transactions"] == 4.0
    assert (hnw["avg_savings_ratio"], hnw["avg_discretionary_ratio"]) == (0.25, 0.5)
    assert by["UNSEGMENTED"]["avg_monthly_spend_minor"] is None


def test_spending_by_segment_excludes_own_account_moves(
    client: TestClient, make_staff: Callable[..., Actor], population: dict[str, Any]
) -> None:
    body = get(client, make_staff(RoleName.RELATIONSHIP_MANAGER))
    assert body["currency"] == "INR" and body["period_days"] == 30 and body["segment"] is None
    by = {s["segment"]: s for s in body["spending"]}
    assert set(by) == {"HIGH_NET_WORTH", "BUDGET_CONSCIOUS", "UNSEGMENTED"}
    assert by["HIGH_NET_WORTH"] == {
        "segment": "HIGH_NET_WORTH", "customers": 2, "spenders": 2, "spend_minor": 22_000,
        "transactions": 3, "avg_spend_per_spender_minor": 11_000,
        "by_type": [{"transaction_type": "TRANSFER", "spend_minor": 22_000, "transactions": 3}],
    }  # fmt: skip
    assert (by["BUDGET_CONSCIOUS"]["spenders"], by["BUDGET_CONSCIOUS"]["spend_minor"]) == (0, 0)
    assert by["UNSEGMENTED"]["by_type"] == [] and by["UNSEGMENTED"]["avg_spend_per_spender_minor"] == 0  # fmt: skip


def test_product_affinity_from_holdings_and_recommendation_clicks(
    client: TestClient, make_staff: Callable[..., Actor], population: dict[str, Any]
) -> None:
    body = get(client, make_staff(RoleName.ADMIN))
    aff = body["affinity"]
    assert aff["products"] == ["SAVINGS_ACCOUNT", "CURRENT_ACCOUNT", "PERSONAL_LOAN", "AUTO_LOAN", "HOME_LOAN"]  # fmt: skip
    held = {s["segment"]: {h["product"]: (h["holders"], h["share_pct"]) for h in s["holdings"]} for s in aff["segments"]}  # fmt: skip
    # every customer opens with a savings account; A opened a current account; B took a loan
    assert held["HIGH_NET_WORTH"] == {
        "SAVINGS_ACCOUNT": (2, 100.0), "CURRENT_ACCOUNT": (1, 50.0), "PERSONAL_LOAN": (1, 50.0),
        "AUTO_LOAN": (0, 0.0), "HOME_LOAN": (0, 0.0),
    }  # fmt: skip
    assert held["BUDGET_CONSCIOUS"]["SAVINGS_ACCOUNT"] == (1, 100.0)
    assert held["UNSEGMENTED"]["CURRENT_ACCOUNT"] == (0, 0.0)
    clicks = [(c["product_id"], c["clicks"], c["customers"], c["product_type"]) for c in aff["recommendation_clicks"]]  # fmt: skip
    assert clicks == [(str(SAVINGS_ACCOUNT_ID), 1, 1, "SAVINGS"), (str(FIXED_DEPOSIT_ID), 1, 1, "SAVINGS")]  # fmt: skip


def test_engagement_from_clickstream_with_period_and_segment_filters(
    client: TestClient, make_staff: Callable[..., Actor], population: dict[str, Any]
) -> None:
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    eng = get(client, rm)["engagement"]
    assert {k: eng[k] for k in ("events", "sessions", "active_customers", "customers")} == {
        "events": 6, "sessions": 3, "active_customers": 3, "customers": 4
    }  # fmt: skip
    assert eng["active_share_pct"] == 75.0 and eng["events_per_session"] == 2.0
    assert eng["avg_session_seconds"] == 50  # (120 + 30 + 0) / 3
    assert {e["event_type"]: e["events"] for e in eng["by_event_type"]} == {
        "page_view": 2, "product_clicked": 3, "account_viewed": 1,
        "loan_calculator_used": 0, "transfer_initiated": 0, "search_performed": 0,
    }  # fmt: skip
    assert [(s["segment"], s["active_customers"], s["active_share_pct"]) for s in eng["by_segment"]] == [
        ("HIGH_NET_WORTH", 1, 50.0), ("BUDGET_CONSCIOUS", 1, 100.0), ("UNSEGMENTED", 1, 100.0)
    ]  # fmt: skip
    assert eng["daily"] == [{"day": population["day"], "active_customers": 3, "events": 6}]

    wide = get(client, rm, "?days=90")["engagement"]  # A's session 40 days ago comes in
    assert (wide["events"], wide["sessions"], wide["avg_session_seconds"]) == (7, 4, 38)  # 150/4

    hnw = get(client, rm, "?segment=HIGH_NET_WORTH")
    assert hnw["segment"] == "HIGH_NET_WORTH"
    e = hnw["engagement"]
    assert (e["events"], e["sessions"], e["active_customers"], e["customers"], e["active_share_pct"]) == (3, 1, 1, 2, 50.0)  # fmt: skip
    assert [s["segment"] for s in hnw["spending"]] == ["HIGH_NET_WORTH"]
    assert [c["product_id"] for c in hnw["affinity"]["recommendation_clicks"]] == [str(SAVINGS_ACCOUNT_ID)]  # fmt: skip
    assert hnw["segments"]["total_customers"] == 4  # the mix is never filtered

    none = get(client, rm, "?segment=INACTIVE_SAVER")
    assert none["spending"] == [] and none["engagement"]["customers"] == 0
    assert none["engagement"]["events_per_session"] == 0.0 and none["engagement"]["daily"] == []


def test_empty_bank_gives_zeros_not_errors(client: TestClient, make_staff: Callable[..., Actor]) -> None:  # fmt: skip
    body = get(client, make_staff(RoleName.RELATIONSHIP_MANAGER))
    assert (
        body["segments"]["total_customers"] == 0 and body["segments"]["last_segmented_at"] is None
    )
    assert all(i["share_pct"] == 0.0 for i in body["segments"]["items"])
    assert body["spending"] == [] and body["affinity"]["segments"] == []
    assert body["engagement"]["events"] == 0 and body["engagement"]["avg_session_seconds"] == 0


def test_behaviour_needs_rm_or_admin_with_mfa_and_validates_filters(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor]
) -> None:
    assert client.get(URL).status_code == 401
    for actor in (make_customer(), make_staff(RoleName.FRAUD_ANALYST), make_staff(RoleName.COMPLIANCE_OFFICER)):  # fmt: skip
        assert client.get(URL, headers=actor.headers).status_code == 403
    denied = client.get(URL, headers=make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=False).headers)
    assert denied.status_code == 403 and denied.json()["code"] == "mfa_required"
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    for query in ("?days=6", "?days=181", "?segment=VIP", "?segment=high_net_worth"):
        assert client.get(f"{URL}{query}", headers=rm.headers).status_code == 422, query


def test_behaviour_response_is_aggregate_only(
    client: TestClient, make_staff: Callable[..., Actor], population: dict[str, Any]
) -> None:
    text_body = client.get(URL, headers=make_staff(RoleName.ADMIN).headers).text
    for actor in (population["a"], population["b"], population["c"], population["d"]):
        for value in (
            actor.email,
            str(actor.customer_id),
            str(actor.user_id),
            actor.account_number,
        ):
            assert value and value not in text_body, value
    for value in ("Test Customer", "dinner", "s-a1", "/insights", "not-in-catalogue"):
        assert value not in text_body, value
