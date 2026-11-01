"""RM insights additions for the dashboard (TASK-066, ADR-0022).

- the segment comes from the offline job's ``customer_segments`` table (regression)
- loan propensity per row and a server-side ``min_loan_propensity`` filter that pages correctly
- the customer detail: exact 30-day volume, health score, eligible products, no extra PII, 404
- the outreach log: RM/admin with MFA only, validated, audited without the note, 404 for unknown
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]

LIST = "/api/v1/insights/customers"


def _detail(customer_id: uuid.UUID | str) -> str:
    return f"{LIST}/{customer_id}"


def _log(customer_id: uuid.UUID | str) -> str:
    return f"{LIST}/{customer_id}/interactions"


NOTE = {"channel": "CALL", "outcome": "FOLLOW_UP", "note": "Asked about a credit line; call back."}


def _assign_segment(db: Session, customer_id: uuid.UUID, segment: str) -> None:
    db.execute(
        text(
            "INSERT INTO customer_segments (id, customer_id, segment, confidence_score, recency_days, "
            "frequency, monetary_minor, savings_ratio, discretionary_ratio, explanation, "
            "updated_at) VALUES (gen_random_uuid(), :c, :s, 0.9, 1, 4.0, 1000, 0.2, 0.1, 'test', now())"
        ),
        {"c": customer_id, "s": segment},
    )
    db.commit()


def test_segment_filter_uses_the_segmentation_job_output(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    """Regression: rows read ``customers.segment``, which nothing writes, so every row was
    UNSEGMENTED and the segment filter never matched a real segment."""
    saver = make_customer(funds=10_000_00)
    make_customer(funds=10_000_00)
    assert saver.customer_id is not None
    _assign_segment(db, saver.customer_id, "INACTIVE_SAVER")
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)

    body = client.get(f"{LIST}?segment=INACTIVE_SAVER", headers=rm.headers).json()
    assert body["total"] == 1
    assert body["items"][0]["customer_id"] == str(saver.customer_id)
    assert body["items"][0]["segment"] == "INACTIVE_SAVER"
    rest = client.get(f"{LIST}?segment=UNSEGMENTED", headers=rm.headers).json()
    assert rest["total"] == 1


def test_loan_propensity_is_reported_and_filtered_on_the_server(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor]
) -> None:
    rich = make_customer(funds=200_000_00)  # one month of 2,00,000 income: a loan is eligible
    make_customer(funds=100)  # too little income for any loan product
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)

    rows = {i["customer_id"]: i for i in client.get(LIST, headers=rm.headers).json()["items"]}
    loan_score = rows[str(rich.customer_id)]["loan_propensity_score"]
    assert isinstance(loan_score, float) and 0 < loan_score <= 1
    assert rows[str(rich.customer_id)]["loan_product"]
    poor = next(r for cid, r in rows.items() if cid != str(rich.customer_id))
    assert (poor["loan_product"], poor["loan_propensity_score"]) == (None, None)

    any_loan = client.get(f"{LIST}?min_loan_propensity=0", headers=rm.headers).json()
    assert any_loan["total"] == 1  # no eligible loan product is never a match
    at = client.get(f"{LIST}?min_loan_propensity={loan_score}", headers=rm.headers).json()
    assert [i["customer_id"] for i in at["items"]] == [str(rich.customer_id)]
    if loan_score < 1:
        above = client.get(
            f"{LIST}?min_loan_propensity={min(loan_score + 0.001, 1.0)}", headers=rm.headers
        ).json()
        assert above == {**above, "items": [], "total": 0}
    page_two = client.get(f"{LIST}?min_loan_propensity=0&offset=1", headers=rm.headers).json()
    assert (page_two["items"], page_two["total"]) == ([], 1)
    assert client.get(f"{LIST}?min_loan_propensity=1.5", headers=rm.headers).status_code == 422


def _pay(
    client: TestClient,
    payer: Actor,
    payee_owner: Actor,
    make_verified_payee: Callable[..., dict[str, Any]],
    engine: Engine,
    amount: int,
) -> None:
    payee = make_verified_payee(payer, payee_owner)
    age_payee(engine, payee["id"], 48)
    resp = client.post(
        "/api/v1/transfers",
        headers={**payer.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(payer.account_id),
            "amount_minor": amount,
            "payee_id": payee["id"],
            "reference": "Rent",
        },
    )
    assert resp.status_code == 201, resp.text


def test_detail_has_exact_volume_health_and_products_and_no_extra_pii(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice = make_customer(funds=100_000_00)
    bob = make_customer()
    _pay(client, alice, bob, make_verified_payee, migrated_engine, 40_000_00)
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)

    resp = client.get(_detail(alice.customer_id), headers=rm.headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "insight",
        "customer_since",
        "health",
        "transaction_volume",
        "recommendations",
        "evaluated_at",
    }
    assert body["insight"]["customer_id"] == str(alice.customer_id)
    assert body["insight"]["total_balance_minor"] == 60_000_00
    assert body["transaction_volume"] == {
        "window_days": 30,
        "transaction_count": 2,
        "inflow_minor": 100_000_00,
        "outflow_minor": 40_000_00,
    }
    assert set(body["health"]) == {"overall_score", "rating", "insufficient_data"}
    assert 0 <= body["health"]["overall_score"] <= 100
    scores = [p["score"] for p in body["recommendations"]]
    assert scores == sorted(scores, reverse=True)
    assert all(p["reasons"] for p in body["recommendations"])
    flat = resp.text
    assert alice.email not in flat and (alice.account_number or "-") not in flat

    bob_volume = client.get(_detail(bob.customer_id), headers=rm.headers).json()
    assert bob_volume["transaction_volume"]["transaction_count"] == 1
    assert bob_volume["transaction_volume"]["inflow_minor"] == 40_000_00
    assert bob_volume["transaction_volume"]["outflow_minor"] == 0


def test_detail_of_unknown_customer_is_404(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    assert client.get(_detail(uuid.uuid4()), headers=rm.headers).status_code == 404
    assert client.get(_detail("not-a-uuid"), headers=rm.headers).status_code == 422


@pytest.mark.parametrize("route", ["detail", "list_log", "post_log"])
def test_new_routes_need_rm_or_admin_with_mfa(
    route: str,
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    alice = make_customer(funds=1_000_00)

    def call(headers: dict[str, str] | None) -> int:
        if route == "detail":
            return client.get(_detail(alice.customer_id), headers=headers).status_code
        if route == "list_log":
            return client.get(_log(alice.customer_id), headers=headers).status_code
        return client.post(_log(alice.customer_id), headers=headers, json=NOTE).status_code

    ok = 201 if route == "post_log" else 200
    assert call(None) == 401
    assert call(alice.headers) == 403  # the customer cannot read the RM's notes about them
    assert call(make_staff(RoleName.FRAUD_ANALYST).headers) == 403
    assert call(make_staff(RoleName.COMPLIANCE_OFFICER).headers) == 403
    assert call(make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=False).headers) == 403
    assert call(make_staff(RoleName.RELATIONSHIP_MANAGER).headers) == ok
    assert call(make_staff(RoleName.ADMIN).headers) == ok


def test_logged_interaction_is_listed_shared_and_audited_without_the_note(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer(funds=1_000_00)
    other = make_customer(funds=1_000_00)
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    colleague = make_staff(RoleName.RELATIONSHIP_MANAGER)

    created = client.post(
        _log(alice.customer_id), headers=rm.headers, json={**NOTE, "note": "  Spoke today.  "}
    )
    assert created.status_code == 201, created.text
    out = created.json()
    assert (out["channel"], out["outcome"], out["note"]) == ("CALL", "FOLLOW_UP", "Spoke today.")
    assert out["logged_by"] == rm.email
    second = client.post(
        _log(alice.customer_id), headers=colleague.headers, json={**NOTE, "channel": "EMAIL"}
    )
    assert second.status_code == 201

    listed = client.get(_log(alice.customer_id), headers=colleague.headers).json()
    assert listed["total"] == 2
    assert {i["logged_by"] for i in listed["items"]} == {rm.email, colleague.email}
    one = client.get(f"{_log(alice.customer_id)}?limit=1", headers=rm.headers).json()
    assert (len(one["items"]), one["total"]) == (1, 2)
    assert client.get(_log(other.customer_id), headers=rm.headers).json() == {
        "items": [],
        "total": 0,
    }  # notes are per customer

    audit = db.execute(
        text(
            "SELECT actor_user_id, resource_id, details FROM audit_logs "
            "WHERE action = 'RM_INTERACTION_LOGGED' ORDER BY seq"
        )
    ).all()
    assert [(a.actor_user_id, a.resource_id) for a in audit] == [
        (rm.user_id, str(alice.customer_id)),
        (colleague.user_id, str(alice.customer_id)),
    ]
    assert audit[0].details == {
        "interaction_id": out["id"],
        "channel": "CALL",
        "outcome": "FOLLOW_UP",
    }  # the free-text note never goes to the audit trail


@pytest.mark.parametrize(
    "body",
    [
        {**NOTE, "note": ""},
        {**NOTE, "note": "   "},
        {**NOTE, "note": "x" * 1001},
        {**NOTE, "channel": "SMS"},
        {**NOTE, "outcome": "SOLD"},
        {**NOTE, "customer_id": str(uuid.uuid4())},  # unknown fields are rejected
        {"channel": "CALL", "outcome": "FOLLOW_UP"},
    ],
)
def test_invalid_interactions_are_rejected_and_nothing_is_stored(
    body: dict[str, Any],
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer()
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    assert client.post(_log(alice.customer_id), headers=rm.headers, json=body).status_code == 422
    assert db.execute(text("SELECT count(*) FROM rm_interactions")).scalar_one() == 0


def test_interactions_for_an_unknown_customer_are_404_and_not_stored(
    client: TestClient, make_staff: Callable[..., Actor], db: Session
) -> None:
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    ghost = uuid.uuid4()
    assert client.post(_log(ghost), headers=rm.headers, json=NOTE).status_code == 404
    assert client.get(_log(ghost), headers=rm.headers).status_code == 404
    assert db.execute(text("SELECT count(*) FROM rm_interactions")).scalar_one() == 0
    assert (
        db.execute(
            text("SELECT count(*) FROM audit_logs WHERE action = 'RM_INTERACTION_LOGGED'")
        ).scalar_one()
        == 0
    )
