"""TASK-096 / ADR-0028: the migration-seeded insurance catalogue (PostgreSQL)."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_test_support import Actor
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

URL = "/api/v1/wealth/insurance"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
LAKH = 1_00_000_00
CRORE = 100 * LAKH
ALL_BANDS_FROM_18 = [
    {"age_band": "18-25", "whole_band_eligible": True},
    {"age_band": "26-35", "whole_band_eligible": True},
    {"age_band": "36-50", "whole_band_eligible": True},
    {"age_band": "51-59", "whole_band_eligible": True},
    {"age_band": "60+", "whole_band_eligible": False},
]

# The exact seeded catalogue (migration 0019): changing a premium or a limit is a NEW migration.
EXPECTED: dict[str, dict[str, Any]] = {
    "TERM-LIFE-CLASSIC": {
        "id": "30000000-0000-4000-a000-000000000001",
        "insurance_type": "TERM_LIFE",
        "cover_min_minor": 25 * LAKH,
        "cover_max_minor": 5 * CRORE,
        "policy_terms_years": [10, 15, 20, 25, 30, 35, 40],
        "indicative_premium": {
            "annual_premium_minor": 12_500_00,
            "reference_cover_minor": 1 * CRORE,
            "reference_age": 30,
            "reference_term_years": 30,
        },
        "eligibility": {
            "min_entry_age": 18,
            "max_entry_age": 60,
            "max_cover_age": 75,
            "age_bands": ALL_BANDS_FROM_18,
        },
    },
    "TERM-LIFE-SMALL": {
        "id": "30000000-0000-4000-a000-000000000002",
        "insurance_type": "TERM_LIFE",
        "cover_min_minor": 10 * LAKH,
        "cover_max_minor": 50 * LAKH,
        "policy_terms_years": [5, 10, 15, 20],
        "indicative_premium": {
            "annual_premium_minor": 7_200_00,
            "reference_cover_minor": 25 * LAKH,
            "reference_age": 40,
            "reference_term_years": 20,
        },
        "eligibility": {
            "min_entry_age": 18,
            "max_entry_age": 65,
            "max_cover_age": 75,
            "age_bands": ALL_BANDS_FROM_18,
        },
    },
    "HEALTH-INDIVIDUAL": {
        "id": "30000000-0000-4000-a000-000000000003",
        "insurance_type": "HEALTH",
        "cover_min_minor": 3 * LAKH,
        "cover_max_minor": 1 * CRORE,
        "policy_terms_years": [1],
        "indicative_premium": {
            "annual_premium_minor": 7_800_00,
            "reference_cover_minor": 5 * LAKH,
            "reference_age": 30,
            "reference_term_years": 1,
        },
        "eligibility": {
            "min_entry_age": 18,
            "max_entry_age": 65,
            "max_cover_age": None,
            "age_bands": ALL_BANDS_FROM_18,
        },
    },
    "HEALTH-SENIOR": {
        "id": "30000000-0000-4000-a000-000000000004",
        "insurance_type": "HEALTH",
        "cover_min_minor": 3 * LAKH,
        "cover_max_minor": 25 * LAKH,
        "policy_terms_years": [1],
        "indicative_premium": {
            "annual_premium_minor": 32_000_00,
            "reference_cover_minor": 5 * LAKH,
            "reference_age": 65,
            "reference_term_years": 1,
        },
        "eligibility": {
            "min_entry_age": 60,
            "max_entry_age": 75,
            "max_cover_age": None,
            "age_bands": [{"age_band": "60+", "whole_band_eligible": False}],
        },
    },
}


def _catalogue(client: TestClient, actor: Actor, query: str = "") -> Any:
    resp = client.get(URL + query, headers=actor.headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_catalogue_returns_the_exact_seeded_term_life_and_health_products(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    products = _catalogue(client, make_customer())
    # ordered by type (TERM_LIFE sorts after HEALTH), then code
    assert [p["code"] for p in products] == [
        "HEALTH-INDIVIDUAL",
        "HEALTH-SENIOR",
        "TERM-LIFE-CLASSIC",
        "TERM-LIFE-SMALL",
    ]
    for product in products:
        expected = EXPECTED[product["code"]]
        for field, value in expected.items():
            assert product[field] == value, (product["code"], field)
        assert product["currency"] == "INR" and product["catalogue_version"] == "2026-11-v1"
        # labelled honestly: indicative, simulated, no underwriting
        assert "indicative, simulated" in product["notes"].lower()
        assert "no underwriting" in product["notes"].lower()
        assert "(illustrative)" in product["name"]
        premium = product["indicative_premium"]
        assert all(isinstance(v, int) for v in premium.values())  # integer minor units
        assert product["cover_min_minor"] <= premium["reference_cover_minor"]
        assert premium["reference_cover_minor"] <= product["cover_max_minor"]


@pytest.mark.parametrize(
    ("insurance_type", "codes"),
    [
        ("TERM_LIFE", ["TERM-LIFE-CLASSIC", "TERM-LIFE-SMALL"]),
        ("HEALTH", ["HEALTH-INDIVIDUAL", "HEALTH-SENIOR"]),
    ],
)
def test_catalogue_filters_by_insurance_type(
    client: TestClient,
    make_customer: Callable[..., Actor],
    insurance_type: str,
    codes: list[str],
) -> None:
    products = _catalogue(client, make_customer(), f"?insurance_type={insurance_type}")
    assert [p["code"] for p in products] == codes


def test_catalogue_rejects_an_unknown_insurance_type(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    resp = client.get(f"{URL}?insurance_type=MOTOR", headers=make_customer().headers)
    assert resp.status_code == 422


def test_hints_are_not_underwriting_every_customer_sees_the_same_catalogue(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    """The endpoint reads nothing about the caller: a 70-year-old who consented to the use of
    their age band and a customer with no date of birth get byte-identical answers."""
    senior, unknown = make_customer(), make_customer()
    dob = client.put(
        "/api/v1/customers/me/date-of-birth",
        headers=senior.headers,
        json={"date_of_birth": "1956-01-01"},
    )
    assert dob.status_code == 200 and dob.json()["age_band"] == "60+"
    consent = client.put(
        "/api/v1/customers/me/recommendations-consent",
        headers=senior.headers,
        json={"granted": True},
    )
    assert consent.status_code == 200, consent.text
    assert (
        client.get(URL, headers=senior.headers).content
        == client.get(URL, headers=unknown.headers).content
    )


def test_insurance_catalogue_is_customer_only(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    assert client.get(URL).status_code == 401
    for role in (RoleName.FRAUD_ANALYST, RoleName.ADMIN):
        assert client.get(URL, headers=make_staff(role).headers).status_code == 403, role


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("reference_age", 61),  # outside TERM-LIFE-CLASSIC's entry ages (18 to 60)
        ("reference_cover_minor", 6 * CRORE),  # above its cover range
        ("reference_term_years", 12),  # a term it does not offer
        ("min_entry_age", 17),  # minors cannot be covered
        ("max_cover_age", 50),  # cover would end before the oldest entry age
        ("indicative_annual_premium_minor", 0),
        ("insurance_type", "MOTOR"),
    ],
)
def test_the_database_refuses_an_inconsistent_catalogue_row(
    migrated_engine: Engine, column: str, value: object
) -> None:
    with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
        connection.execute(
            text(f"UPDATE insurance_products SET {column} = :value WHERE code = :code"),  # noqa: S608 - column from the fixed parametrize list
            {"value": value, "code": "TERM-LIFE-CLASSIC"},
        )


def test_migration_downgrade_drops_the_catalogue_and_upgrade_reseeds_it(
    migrated_engine: Engine, test_database_url: str
) -> None:
    config = Config(str(ALEMBIC_INI))
    os.environ["ALEMBIC_DATABASE_URL"] = test_database_url
    try:
        command.downgrade(config, "0018")
        assert "insurance_products" not in inspect(migrated_engine).get_table_names()
        assert "wealth_products" in inspect(migrated_engine).get_table_names()
    finally:
        command.upgrade(config, "head")
    with migrated_engine.connect() as connection:
        codes = connection.execute(
            text("SELECT code FROM insurance_products ORDER BY code")
        ).scalars()
        assert list(codes) == sorted(EXPECTED)
