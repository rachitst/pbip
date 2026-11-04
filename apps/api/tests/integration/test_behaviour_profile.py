"""UC-11 behavioural profile and analytics consent (ADR-0057).

- consent is off by default, recorded with a timestamp and policy version, and audited; repeating
  a decision writes nothing; turning it off erases the customer's events and profile
- without consent the events API stores nothing (both sides tested)
- the daily job derives sessions (30-minute timeout, out-of-order input), engagement and product
  interests for consenting customers only, and is idempotent
- the customer reads only their own profile; staff need RM or ADMIN with MFA, and an RM only sees
  assigned customers (others are 404, like a customer that does not exist)
- the staff aggregates count consenting customers' clickstream only and split sessions by the
  same rule as the job
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.analytics.profile_job import run_behaviour_profile_job
from pbip_api.modules.auth.models import RoleName
from pbip_ml.recommendations.engine import CREDIT_LINE_ID
from pbip_test_support import Actor, FakeClock, grant_analytics_consent
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]

CONSENT = "/api/v1/customers/me/analytics-consent"
ME = "/api/v1/analytics/behaviour-profile/me"
BATCH = "/api/v1/events/batch"


def staff_url(customer: Actor) -> str:
    return f"/api/v1/insights/customers/{customer.customer_id}/behaviour-profile"


def post_events(client: TestClient, actor: Actor, events: list[tuple[str, datetime, str, str | None, dict[str, str]]]) -> dict[str, Any]:  # fmt: skip
    payload = {
        "events": [
            {"event_type": t, "event_timestamp": at.isoformat(), "session_id": s,
             **({"page_url": page} if page else {}), "properties": props}
            for t, at, s, page, props in events
        ]
    }  # fmt: skip
    r = client.post(BATCH, headers=actor.headers, json=payload)
    assert r.status_code == 202, r.text
    return dict(r.json())


def stored_events(engine: Engine, customer: Actor) -> int:
    with engine.connect() as c:
        return int(
            c.execute(
                text("SELECT count(*) FROM clickstream_events WHERE customer_id = :c"),
                {"c": customer.customer_id},
            ).scalar_one()
        )


def audit_rows(engine: Engine, customer: Actor, action: str) -> list[Any]:
    with engine.connect() as c:
        return list(
            c.execute(
                text(
                    "SELECT actor_user_id, details FROM audit_logs WHERE action = :a "
                    "AND resource_id = :r ORDER BY seq"
                ),
                {"a": action, "r": str(customer.customer_id)},
            ).all()
        )


def insert_legacy_event(engine: Engine, customer: Actor, at: datetime) -> None:
    """An event stored before consent existed (TASK-112 recorded without one)."""
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO clickstream_events (id, event_timestamp, event_type, customer_id, "
                "session_id, page_url, properties) VALUES (gen_random_uuid(), :at, 'page_view', "
                ":c, 'legacy', '/loans', '{}'::jsonb)"
            ),
            {"at": at, "c": customer.customer_id},
        )


BASE = (datetime.now(UTC) - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
LOAN_CLICK = {"product_id": str(CREDIT_LINE_ID)}


def a_visit(client: TestClient, actor: Actor) -> None:
    """Hand-worked profile (job run at BASE + 2 days):

    s1 run 1: /loans page view, heartbeat, loan card click, calculator over 120 s -> 3 interactions,
    1 page view; 42 minutes later (gap > 30) a /accounts page view -> a second session of 0 s;
    s2: one /wealth page view 7 days earlier -> 0 s. Sent out of order.
    Sessions 3, mean 40 s, 3 page views, 5 interactions, last active BASE + 42 min.
    LOAN = (1 + 3 + 2) * 0.5 ** (2 / 14) ~ 5.434 (3 signals); INVESTMENT = 0.5 ** (9 / 14) ~ 0.640.
    """
    post_events(client, actor, [
        ("page_view", BASE + timedelta(minutes=42), "s1", "/accounts", {}),
        ("loan_calculator_used", BASE + timedelta(seconds=120), "s1", "/loans", {"loan_type": "HOME"}),
        ("page_view", BASE, "s1", "/loans", {}),
        ("session_heartbeat", BASE + timedelta(seconds=60), "s1", "/loans", {}),
        ("product_clicked", BASE + timedelta(seconds=90), "s1", "/insights", LOAN_CLICK),
        ("page_view", BASE - timedelta(days=7), "s2", "/wealth", {}),
    ])  # fmt: skip


def test_consent_is_off_by_default_then_recorded_audited_and_not_reaudited(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine, clock: FakeClock
) -> None:  # fmt: skip
    alice = make_customer()
    initial = client.get(CONSENT, headers=alice.headers).json()
    assert initial == {"granted": False, "updated_at": None, "policy_version": None,
                       "current_policy_version": "ADR-0057-v1"}  # fmt: skip
    on = client.put(CONSENT, headers=alice.headers, json={"granted": True})
    assert on.status_code == 200
    assert on.json()["granted"] is True and on.json()["policy_version"] == "ADR-0057-v1"
    assert datetime.fromisoformat(on.json()["updated_at"]) == datetime.fromtimestamp(clock.now, UTC)
    again = client.put(CONSENT, headers=alice.headers, json={"granted": True})
    assert again.json() == on.json()
    [grant] = audit_rows(migrated_engine, alice, "ANALYTICS_CONSENT_GRANTED")
    assert grant.actor_user_id == alice.user_id
    assert grant.details == {"policy_version": "ADR-0057-v1", "purpose": "behavioural_analytics"}


@pytest.mark.parametrize(
    "body", [{"granted": "yes"}, {"granted": 1}, {}, {"granted": True, "x": 1}]
)
def test_consent_input_is_strict(
    client: TestClient, make_customer: Callable[..., Actor], body: dict[str, Any]
) -> None:
    assert client.put(CONSENT, headers=make_customer().headers, json=body).status_code == 422


def test_without_consent_events_are_acknowledged_and_not_stored(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    bob = make_customer()
    result = post_events(client, bob, [("page_view", BASE, "s", "/accounts", {})] * 2)
    assert (result["accepted"], result["duplicates"], result["not_recorded"]) == (0, 0, 2)
    assert stored_events(migrated_engine, bob) == 0
    grant_analytics_consent(client, bob)
    result = post_events(client, bob, [("page_view", BASE, "s", "/accounts", {})])
    assert (result["accepted"], result["not_recorded"]) == (1, 0)
    assert stored_events(migrated_engine, bob) == 1


def test_job_builds_the_profile_and_the_customer_reads_it(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, clock: FakeClock
) -> None:  # fmt: skip
    alice = grant_analytics_consent(client, make_customer(salary=40_000_00))
    a_visit(client, alice)
    before = client.get(ME, headers=alice.headers).json()
    assert before["analytics_consent"] is True
    assert before["profile"]["computed_at"] is None and before["profile"]["engagement"] is None
    assert before["profile"]["segment"] == "UNSEGMENTED"
    assert before["profile"]["spend_income"]["monthly_income_minor"] == 40_000_00

    now = BASE + timedelta(days=2)
    assert run_behaviour_profile_job(db, now).profiled == 1
    body = client.get(ME, headers=alice.headers).json()["profile"]
    assert datetime.fromisoformat(body["computed_at"]) == now
    assert body["engagement"] == {
        "window_days": 90, "sessions": 3, "avg_session_seconds": 40, "pages_per_session": 1.0,
        "interactions": 5, "last_active_at": (BASE + timedelta(minutes=42)).isoformat().replace("+00:00", "Z"),
    }  # fmt: skip
    interests = [(i["category"], i["signals"]) for i in body["product_interests"]]
    assert interests == [("LOAN", 3), ("INVESTMENT", 1)]
    assert body["product_interests"][0]["score"] == pytest.approx(6 * 0.5 ** (2 / 14), abs=2e-3)
    assert body["product_interests"][1]["score"] == pytest.approx(0.5 ** (9 / 14), abs=1e-3)

    assert run_behaviour_profile_job(db, now).profiled == 1  # idempotent re-run
    assert client.get(ME, headers=alice.headers).json()["profile"] == body


def test_the_job_profiles_only_consenting_customers(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, migrated_engine: Engine
) -> None:  # fmt: skip
    keen = grant_analytics_consent(client, make_customer())
    quiet = grant_analytics_consent(client, make_customer())  # consents but never uses the app
    legacy = make_customer()
    insert_legacy_event(migrated_engine, legacy, BASE)
    a_visit(client, keen)
    assert run_behaviour_profile_job(db, BASE + timedelta(days=2)).profiled == 2
    with migrated_engine.connect() as c:
        rows = dict(c.execute(text("SELECT customer_id, sessions FROM customer_behaviour_profiles")).all())  # fmt: skip
    assert rows == {keen.customer_id: 3, quiet.customer_id: 0}
    assert client.get(ME, headers=legacy.headers).json() == {
        "analytics_consent": False, "profile": None
    }  # fmt: skip

    # Safety net: consent switched off without the API (no erasure) -> the job removes the profile.
    with migrated_engine.begin() as c:
        c.execute(text("UPDATE customers SET analytics_consent = false WHERE id = :c"), {"c": quiet.customer_id})  # fmt: skip
    result = run_behaviour_profile_job(db, BASE + timedelta(days=2))
    assert (result.profiled, result.removed) == (1, 1)


def test_withdrawal_erases_events_and_profile_and_is_audited(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, migrated_engine: Engine
) -> None:  # fmt: skip
    alice = grant_analytics_consent(client, make_customer())
    a_visit(client, alice)
    run_behaviour_profile_job(db, BASE + timedelta(days=2))
    off = client.put(CONSENT, headers=alice.headers, json={"granted": False})
    assert off.status_code == 200 and off.json()["granted"] is False
    assert off.json()["updated_at"] is not None  # the refusal is a recorded decision too
    assert stored_events(migrated_engine, alice) == 0
    with migrated_engine.connect() as c:
        left = c.execute(
            text(
                "SELECT (SELECT count(*) FROM customer_behaviour_profiles WHERE customer_id = :c),"
                " (SELECT count(*) FROM customer_product_interests WHERE customer_id = :c)"
            ),
            {"c": alice.customer_id},
        ).one()
    assert tuple(left) == (0, 0)
    [withdrawn] = audit_rows(migrated_engine, alice, "ANALYTICS_CONSENT_WITHDRAWN")
    assert withdrawn.details == {
        "policy_version": "ADR-0057-v1", "purpose": "behavioural_analytics",
        "events_erased": 6, "profile_erased": True,
    }  # fmt: skip
    assert client.get(ME, headers=alice.headers).json() == {"analytics_consent": False, "profile": None}  # fmt: skip
    after = post_events(client, alice, [("page_view", BASE, "s9", "/accounts", {})])
    assert after["not_recorded"] == 1 and stored_events(migrated_engine, alice) == 0


def test_staff_access_needs_rm_or_admin_with_mfa_and_an_rm_sees_assigned_customers_only(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor], db: Session
) -> None:  # fmt: skip
    alice = grant_analytics_consent(client, make_customer())
    other = make_customer()
    a_visit(client, alice)
    run_behaviour_profile_job(db, BASE + timedelta(days=2))
    admin, rm = make_staff(RoleName.ADMIN), make_staff(RoleName.RELATIONSHIP_MANAGER)
    assigned = client.put(
        f"/api/v1/insights/customers/{alice.customer_id}/assignment",
        headers=admin.headers, json={"rm_user_id": str(rm.user_id)},
    )  # fmt: skip
    assert assigned.status_code == 200, assigned.text

    by_admin = client.get(staff_url(alice), headers=admin.headers)
    assert by_admin.status_code == 200 and by_admin.json()["profile"]["engagement"]["sessions"] == 3
    assert client.get(staff_url(alice), headers=rm.headers).json() == by_admin.json()
    # not assigned and not existing look the same
    assert client.get(staff_url(other), headers=rm.headers).status_code == 404
    ghost = f"/api/v1/insights/customers/{uuid.uuid4()}/behaviour-profile"
    assert client.get(ghost, headers=rm.headers).status_code == 404
    assert client.get(ghost, headers=admin.headers).status_code == 404
    # the admin sees the unassigned customer, but without consent there is no profile
    assert client.get(staff_url(other), headers=admin.headers).json() == {"analytics_consent": False, "profile": None}  # fmt: skip

    assert client.get(staff_url(alice)).status_code == 401
    for actor in (alice, make_staff(RoleName.FRAUD_ANALYST)):  # (login limit: few actors per test)
        assert client.get(staff_url(alice), headers=actor.headers).status_code == 403
    no_mfa = make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=False)
    denied = client.get(staff_url(alice), headers=no_mfa.headers)
    assert denied.status_code == 403 and denied.json()["code"] == "mfa_required"
    assert client.get("/api/v1/insights/customers/not-a-uuid/behaviour-profile", headers=admin.headers).status_code == 422  # fmt: skip


def test_staff_aggregates_count_consenting_customers_and_split_sessions_like_the_job(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor],
    db: Session, migrated_engine: Engine,
) -> None:  # fmt: skip
    alice = grant_analytics_consent(client, make_customer())
    legacy = make_customer()
    insert_legacy_event(migrated_engine, legacy, BASE)
    a_visit(client, alice)
    run_behaviour_profile_job(db, BASE + timedelta(days=2))
    body = client.get("/api/v1/insights/behaviour?days=30", headers=make_staff(RoleName.ADMIN).headers).json()  # fmt: skip
    eng = body["engagement"]
    # alice's 5 interactions in 3 sessions (heartbeat not counted); the legacy event is left out
    assert (eng["events"], eng["sessions"], eng["active_customers"], eng["customers"]) == (
        5,
        3,
        1,
        2,
    )
    assert (eng["consenting_customers"], eng["avg_session_seconds"]) == (1, 40)
    assert "session_heartbeat" not in {e["event_type"] for e in eng["by_event_type"]}
    by_day = {d["day"]: (d["sessions"], d["avg_session_seconds"]) for d in eng["daily"]}
    assert by_day == {
        BASE.date().isoformat(): (2, 60), (BASE - timedelta(days=7)).date().isoformat(): (1, 0)
    }  # fmt: skip
    aff = body["affinity"]
    assert aff["profiled_customers"] == 1
    assert [(i["category"], i["customers"], i["share_pct"], i["signals"]) for i in aff["interests"]] == [
        ("INVESTMENT", 1, 100.0, 1), ("LOAN", 1, 100.0, 3)
    ]  # fmt: skip
