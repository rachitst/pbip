"""ADR-0058 section 8: simulated insurance quotes and applications (PostgreSQL)."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.ledger.models import Transaction
from pbip_api.modules.wealth.models import InsuranceApplication
from pbip_test_support import Actor
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

TERM_CLASSIC = "30000000-0000-4000-a000-000000000001"  # migration 0020
HEALTH_SENIOR = "30000000-0000-4000-a000-000000000004"
LAKH = 1_00_000_00
CRORE = 100 * LAKH


def _terms(
    product: str = TERM_CLASSIC,
    cover: int = CRORE,
    term: int = 30,
    band: str = "26-35",
    consent: object = True,
) -> dict[str, Any]:
    return {
        "product_id": product,
        "cover_minor": cover,
        "term_years": term,
        "age_band": band,
        "age_band_consent": consent,
    }


def _apply(client: TestClient, actor: Actor, *, key: str | None = None, **terms: Any) -> Any:
    return client.post(
        "/api/v1/wealth/insurance/applications",
        headers={**actor.headers, "Idempotency-Key": key or uuid.uuid4().hex},
        json=_terms(**terms),
    )


def _count(db: Session, model: type[Any]) -> int:
    return int(db.execute(select(func.count()).select_from(model)).scalar_one())


def test_a_quote_states_the_simulated_premium_and_outcome_and_stores_nothing(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    resp = client.post("/api/v1/wealth/insurance/quote", headers=alice.headers, json=_terms())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["annual_premium_minor"] == 12_500_00  # the reference profile itself
    assert body["outcome"] == "APPROVED_SIMULATED" and body["quote_version"] == "IQ-1"
    assert (body["age_factor_bps"], body["term_factor_bps"]) == (10_000, 10_000)
    declined = client.post(
        "/api/v1/wealth/insurance/quote", headers=alice.headers, json=_terms(band="51-59")
    ).json()
    assert declined["outcome"] == "DECLINED" and declined["annual_premium_minor"] is None
    assert _count(db, InsuranceApplication) == 0


def test_invalid_quotes_are_refused(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    for name, terms in {
        "no consent": _terms(consent=False),
        "consent missing": {k: v for k, v in _terms().items() if k != "age_band_consent"},
        "unknown band": _terms(band="unknown"),
        "cover below the range": _terms(cover=10 * LAKH),
        "cover above the range": _terms(cover=6 * CRORE),
        "term not offered": _terms(term=12),
        "client premium": _terms() | {"annual_premium_minor": 1},
    }.items():
        resp = client.post("/api/v1/wealth/insurance/quote", headers=alice.headers, json=terms)
        assert resp.status_code == 422, f"{name}: {resp.status_code}"
    unknown = _terms(product=str(uuid.uuid4()))
    assert (
        client.post("/api/v1/wealth/insurance/quote", headers=alice.headers, json=unknown)
    ).status_code == 404


@pytest.mark.parametrize(
    ("terms", "status", "premium"),
    [
        ({}, "APPROVED_SIMULATED", 12_500_00),
        ({"band": "36-50"}, "SUBMITTED", 20_000_00),  # at 50 the term passes the cover age
        ({"cover": 3 * CRORE}, "SUBMITTED", 37_500_00),  # above twice the reference cover
        ({"band": "51-59"}, "DECLINED", None),
        ({"product": HEALTH_SENIOR, "cover": 5 * LAKH, "term": 1, "band": "60+"}, "SUBMITTED",
         32_000_00),
    ],
)  # fmt: skip
def test_an_application_is_stored_with_its_status_by_the_documented_rules(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
    terms: dict[str, Any],
    status: str,
    premium: int | None,
) -> None:
    alice = make_customer(funds=10 * LAKH)
    resp = _apply(client, alice, **terms)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["status"], body["annual_premium_minor"]) == (status, premium)
    assert body["decision_reason"]
    row = db.get(InsuranceApplication, uuid.UUID(body["id"]))
    assert row is not None and row.customer_id == alice.customer_id and row.status == status
    audit = db.execute(
        select(AuditLog).where(AuditLog.action == "INSURANCE_APPLICATION_SUBMITTED")
    ).scalar_one()
    assert audit.actor_user_id == alice.user_id and audit.details["status"] == status
    assert "age_band" not in audit.details  # the declared band stays on the application only
    # No policy is issued, so nothing is charged: no ledger posting at all.
    assert _count(db, Transaction) == 1  # only the test's funding deposit
    listed = client.get("/api/v1/wealth/insurance/applications", headers=alice.headers).json()
    assert [(a["id"], a["status"]) for a in listed] == [(body["id"], status)]


def test_an_application_retry_replays_and_records_once(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    key = uuid.uuid4().hex
    first, retry = _apply(client, alice, key=key), _apply(client, alice, key=key)
    assert first.status_code == retry.status_code == 201
    assert retry.headers.get("Idempotent-Replayed") == "true" and retry.json() == first.json()
    assert _count(db, InsuranceApplication) == 1
    reused = _apply(client, alice, key=key, cover=2 * CRORE)
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reuse"
    no_key = client.post(
        "/api/v1/wealth/insurance/applications", headers=alice.headers, json=_terms()
    )
    assert no_key.status_code == 422
    refused = _apply(client, alice, consent=False)
    assert refused.status_code == 422 and _count(db, InsuranceApplication) == 1
