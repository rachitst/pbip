"""UC-07 (ADR-0056): the RTQ-1 risk band in GET /api/v1/recommendations, against PostgreSQL.

- with no questionnaire answered the index-fund SIP is offered as before and its reasons say the
  band was not considered;
- a LOW or MODERATE band removes the high-risk SIP, a HIGH band keeps it and names the band;
- a new questionnaire answer shows on the very next request (the band is read live, the cached
  part of the context is reused, and it costs no extra statement);
- the assistant still sees the pre-band list, so it can say what it left out as too risky.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.recommendations import service as recommendations_service
from pbip_ml.recommendations.engine import INDEX_SIP_ID, NO_RISK_BAND_REASON
from pbip_test_support import Actor, count_statements, month_start, post_spend_at
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.security]

RECS = "/api/v1/recommendations"
RISK = "/api/v1/customers/me/risk-profile"
SIP = str(INDEX_SIP_ID)

CAUTIOUS = {
    "investment_horizon": "UNDER_1_YEAR",
    "loss_reaction": "SELL_EVERYTHING",
    "investment_goal": "PROTECT_SAVINGS",
    "investment_experience": "SOME",
    "emergency_fund": "THREE_TO_6_MONTHS",
}  # 2 points: LOW
MODERATE = {
    **CAUTIOUS,
    "investment_goal": "STEADY_GROWTH",
    "loss_reaction": "WAIT_AND_HOLD",
    "investment_horizon": "OVER_5_YEARS",
}  # 6 points: MODERATE
BOLD = {
    "investment_horizon": "OVER_5_YEARS",
    "loss_reaction": "INVEST_MORE",
    "investment_goal": "MAXIMUM_GROWTH",
    "investment_experience": "EXTENSIVE",
    "emergency_fund": "OVER_6_MONTHS",
}  # 10 points: HIGH


@pytest.fixture(autouse=True)
def cold_cache() -> None:
    recommendations_service.clear_context_cache()


def _saver(make_customer: Callable[..., Actor], engine: Engine) -> Actor:
    """Salary in, INR 50,000 spent in a closed month: every rule allows the index-fund SIP."""
    saver = make_customer(salary=2_50_000_00)
    post_spend_at(
        engine, saver.account_id, 50_000_00, "Rent", month_start(utcnow(), 1) + timedelta(days=5)
    )
    return saver


def _assess(client: TestClient, actor: Actor, answers: dict[str, str], band: str) -> None:
    resp = client.put(RISK, headers=actor.headers, json=answers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["band"] == band


def _sip(client: TestClient, actor: Actor) -> dict[str, Any] | None:
    """The SIP from the customer route's top 3 (for this saver it ranks first when allowed)."""
    resp = client.get(RECS, headers=actor.headers)
    assert resp.status_code == 200, resp.text
    items: list[dict[str, Any]] = resp.json()["recommendations"]
    return next((item for item in items if item["product_id"] == SIP), None)


def test_without_a_band_the_sip_is_offered_and_the_reason_says_the_band_was_not_used(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    saver = _saver(make_customer, migrated_engine)
    sip = _sip(client, saver)
    assert sip is not None
    assert sip["reasons"][-1] == NO_RISK_BAND_REASON


@pytest.mark.parametrize(("answers", "band"), [(CAUTIOUS, "LOW"), (MODERATE, "MODERATE")])
def test_a_band_below_high_removes_the_high_risk_sip(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    answers: dict[str, str],
    band: str,
) -> None:
    saver = _saver(make_customer, migrated_engine)
    _assess(client, saver, answers, band)
    assert _sip(client, saver) is None
    items = client.get(RECS, headers=saver.headers).json()["recommendations"]
    assert items, "the other eligible products are still recommended"


def test_a_high_band_keeps_the_sip_and_names_the_band_after_the_eligibility_figures(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    saver = _saver(make_customer, migrated_engine)
    _assess(client, saver, BOLD, "HIGH")
    sip = _sip(client, saver)
    assert sip is not None
    reasons = sip["reasons"]
    assert reasons[0].startswith("Your balance of INR ")
    assert reasons[-1] == (
        "Your risk profile from the questionnaire is high; this is a high-risk product, so it "
        "fits that profile."
    )


def test_a_new_answer_shows_on_the_next_request_with_the_cached_part_reused(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    """The band is read live from the customer row the request already loaded: the warm request
    after a new answer sends the same 5 statements as any warm request and reads no entries."""
    saver = _saver(make_customer, migrated_engine)
    _assess(client, saver, BOLD, "HIGH")
    assert _sip(client, saver) is not None  # cold: builds and caches the context
    _assess(client, saver, CAUTIOUS, "LOW")
    with count_statements(migrated_engine) as sent:
        assert _sip(client, saver) is None
    assert len(sent) == 5, sent
    assert not any("FROM transaction_entries" in sql for sql in sent)


def test_the_assistant_path_gets_the_pre_band_list_without_a_band_reason(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    saver = _saver(make_customer, migrated_engine)
    _assess(client, saver, CAUTIOUS, "LOW")
    assert saver.customer_id is not None
    with sessionmaker(bind=migrated_engine)() as session:
        found = recommendations_service.get_customer_recommendations(
            session, saver.customer_id, utcnow(), top_k=6, apply_risk_band=False
        )
    sip = next(r for r in found.recommendations if r.product_id == INDEX_SIP_ID)
    assert NO_RISK_BAND_REASON not in sip.reasons
    assert not any("risk profile" in reason for reason in sip.reasons)
