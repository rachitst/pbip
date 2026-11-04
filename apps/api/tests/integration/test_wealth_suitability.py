"""ADR-0058 section 9: products marked suitable or unsuitable for the customer's RTQ-1 band."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_test_support import Actor

pytestmark = pytest.mark.integration

URL = "/api/v1/wealth/suitability"
MODERATE_ANSWERS = {
    "investment_horizon": "UNDER_1_YEAR",
    "loss_reaction": "WAIT_AND_HOLD",
    "investment_goal": "STEADY_GROWTH",
    "investment_experience": "SOME",
    "emergency_fund": "THREE_TO_6_MONTHS",
}  # 0+1+1+1+1 = 4 -> MODERATE (test_risk_profile.py)
FD_REGULAR = "20000000-0000-4000-a000-000000000002"  # LOW
MF_BALANCED = "20000000-0000-4000-a000-000000000005"  # MODERATE
MF_INDEX = "20000000-0000-4000-a000-000000000006"  # HIGH
STK_SIMINFRA = "50000000-0000-4000-a000-000000000004"  # HIGH
TERM_CLASSIC = "30000000-0000-4000-a000-000000000001"  # insurance


def test_without_a_band_nothing_is_rated_and_the_page_is_told_why(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    body = client.get(URL, headers=alice.headers).json()
    assert body["band"] is None and body["assessed_at"] is None
    assert body["items"] and all(i["suitable"] is None for i in body["items"])
    rated = [i for i in body["items"] if i["kind"] != "INSURANCE"]
    assert all("No risk profile recorded" in i["reason"] for i in rated)


def test_products_at_or_below_the_band_are_suitable_and_above_it_are_not(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    put = client.put(
        "/api/v1/customers/me/risk-profile", headers=alice.headers, json=MODERATE_ANSWERS
    )
    assert put.status_code == 200 and put.json()["band"] == "MODERATE"
    body = client.get(URL, headers=alice.headers).json()
    assert body["band"] == "MODERATE" and body["questionnaire_version"] == "RTQ-1"
    by_id = {i["product_id"]: i for i in body["items"]}
    assert by_id[FD_REGULAR]["suitable"] is True and by_id[MF_BALANCED]["suitable"] is True
    assert by_id[MF_INDEX]["suitable"] is False
    assert by_id[MF_INDEX]["reason"] == "High risk is above your moderate risk band."
    assert by_id[STK_SIMINFRA]["kind"] == "INSTRUMENT" and by_id[STK_SIMINFRA]["suitable"] is False
    assert by_id[TERM_CLASSIC]["kind"] == "INSURANCE" and by_id[TERM_CLASSIC]["suitable"] is None
    assert by_id[TERM_CLASSIC]["risk_level"] is None


def test_suitability_is_customer_only(client: TestClient, make_staff: Callable[..., Actor]) -> None:
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=make_staff(RoleName.FRAUD_ANALYST).headers).status_code == 403
