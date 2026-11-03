"""IMPOSSIBLE_TRAVEL arithmetic, thresholds and the city-level resolver (ADR-0034)."""

import math
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pbip_api.core.config import Settings
from pbip_api.core.geoip import (
    GeoPoint,
    MmdbLocationResolver,
    NoLocationResolver,
    location_resolver_for,
)
from pbip_api.modules.fraud.travel import (
    HALF_CIRCUMFERENCE_KM,
    PriorLocation,
    TravelPolicy,
    _reason,
    great_circle_km,
    measure,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

LONDON = GeoPoint(51.5143, -0.0912, "GB", "London")
MUMBAI = GeoPoint(19.0760, 72.8777, "IN", "Mumbai")
AT = datetime(2026, 11, 3, 2, 40, tzinfo=UTC)


class _FakeReader:
    def __init__(self, records: dict[str, object]) -> None:
        self.records = records
        self.asked: list[str] = []

    def get(self, ip_address: str) -> object:
        self.asked.append(ip_address)
        return self.records.get(ip_address)


def _policy(max_speed: float = 900.0, min_distance: float = 500.0) -> TravelPolicy:
    return TravelPolicy(NoLocationResolver(), max_speed, min_distance)


def test_great_circle_matches_known_distances() -> None:
    # One degree of longitude on the equator is 2 * pi * R / 360 = 111.195 km.
    assert great_circle_km(GeoPoint(0, 0), GeoPoint(0, 1)) == pytest.approx(111.195, abs=0.001)
    assert great_circle_km(LONDON, LONDON) == 0
    assert great_circle_km(GeoPoint(0, 0), GeoPoint(0, 180)) == pytest.approx(HALF_CIRCUMFERENCE_KM)
    assert 7_150 < great_circle_km(LONDON, MUMBAI) < 7_250  # issue #143 example: about 7,200 km


def test_two_hours_london_to_mumbai_is_impossible_and_fourteen_hours_is_not() -> None:
    fast = measure(PriorLocation("Sign-in", AT - timedelta(hours=2), LONDON), MUMBAI, AT)
    assert fast.elapsed_seconds == 7200
    assert fast.speed_kmh == pytest.approx(fast.distance_km / 2)
    assert fast.triggers(_policy())
    slow = measure(PriorLocation("Sign-in", AT - timedelta(hours=14), LONDON), MUMBAI, AT)
    assert slow.speed_kmh < 900 and not slow.triggers(_policy())


def test_speed_exactly_at_the_threshold_does_not_trigger_and_one_above_does() -> None:
    travel = measure(PriorLocation("Sign-in", AT - timedelta(hours=3), LONDON), MUMBAI, AT)
    assert not travel.triggers(_policy(max_speed=travel.speed_kmh))
    assert travel.triggers(_policy(max_speed=travel.speed_kmh - 1))


def test_distance_exactly_at_the_minimum_does_not_trigger_and_one_above_does() -> None:
    travel = measure(PriorLocation("Sign-in", AT, LONDON), MUMBAI, AT)
    assert not travel.triggers(_policy(min_distance=travel.distance_km))
    assert travel.triggers(_policy(min_distance=travel.distance_km - 1))


def test_zero_elapsed_time_is_infinite_speed_without_a_division_error() -> None:
    travel = measure(PriorLocation("Sign-in", AT, LONDON), MUMBAI, AT)
    assert travel.elapsed_seconds == 0 and math.isinf(travel.speed_kmh)
    assert travel.triggers(_policy())
    same_place = measure(PriorLocation("Sign-in", AT, LONDON), LONDON, AT)
    assert same_place.speed_kmh == 0 and not same_place.triggers(_policy())


def test_a_prior_event_stamped_after_now_counts_as_no_time_not_negative() -> None:
    travel = measure(PriorLocation("Sign-in", AT + timedelta(minutes=3), LONDON), MUMBAI, AT)
    assert travel.elapsed_seconds == 0 and math.isinf(travel.speed_kmh)


def test_lookback_is_the_time_the_farthest_trip_takes_at_the_speed_limit() -> None:
    assert _policy(max_speed=900).lookback == timedelta(hours=HALF_CIRCUMFERENCE_KM / 900)
    assert timedelta(hours=22) < _policy(max_speed=900).lookback < timedelta(hours=23)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fraud_travel_max_speed_kmh", 0),
        ("fraud_travel_max_speed_kmh", -900),
        ("fraud_travel_max_speed_kmh", float("nan")),
        ("fraud_travel_max_speed_kmh", float("inf")),
        ("fraud_travel_min_distance_km", 0),
        ("fraud_travel_min_distance_km", -1),
        ("fraud_travel_min_distance_km", float("nan")),
        ("fraud_travel_min_distance_km", 20_001),
    ],
)
def test_travel_thresholds_must_be_positive_and_bounded(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})  # type: ignore[arg-type]  # dynamic field


def test_travel_defaults_and_policy_from_settings() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # pydantic-settings kwarg
    assert settings.fraud_travel_max_speed_kmh == 900
    assert settings.fraud_travel_min_distance_km == 500
    assert settings.geoip_city_db_path is None
    policy = TravelPolicy.from_settings(settings, NoLocationResolver())
    assert (policy.max_speed_kmh, policy.min_distance_km) == (900, 500)
    assert TravelPolicy.disabled().max_speed_kmh == 900


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_geoip_path_in_the_environment_means_no_database(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """``GEOIP_CITY_DB_PATH=`` from .env.example must disable the rule exactly like an unset path
    (it came through as ``''`` on Windows and broke the defaults test above). The country path
    gets the same treatment so NEW_COUNTRY behaves the same way."""
    monkeypatch.setenv("GEOIP_CITY_DB_PATH", blank)
    monkeypatch.setenv("GEOIP_COUNTRY_DB_PATH", blank)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # pydantic-settings kwarg
    assert settings.geoip_city_db_path is None
    assert settings.geoip_country_db_path is None


def test_a_set_geoip_path_is_kept_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOIP_CITY_DB_PATH", "/data/geoip/dbip-city-lite.mmdb")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # pydantic-settings kwarg
    assert settings.geoip_city_db_path == "/data/geoip/dbip-city-lite.mmdb"


def test_no_city_database_locates_nothing() -> None:
    assert NoLocationResolver().locate("81.2.69.142") is None
    assert isinstance(location_resolver_for(None), NoLocationResolver)
    assert isinstance(location_resolver_for(""), NoLocationResolver)


def test_city_record_gives_point_country_and_clean_city_name() -> None:
    reader = _FakeReader(
        {
            "81.2.69.142": {
                "city": {"names": {"en": "Lon\x1bdon"}},
                "country": {"iso_code": "GB"},
                "location": {"latitude": 51.5143, "longitude": -0.0912244},
            }
        }
    )
    point = MmdbLocationResolver(reader).locate("81.2.69.142")
    assert point == GeoPoint(51.5143, -0.0912244, "GB", "London")


@pytest.mark.parametrize("ip", ["10.1.2.3", "127.0.0.1", "::1", "fd00::1", "nope", "", None])
def test_private_loopback_and_malformed_addresses_are_never_located(ip: str | None) -> None:
    reader = _FakeReader({})
    assert MmdbLocationResolver(reader).locate(ip) is None
    assert reader.asked == []


@pytest.mark.parametrize(
    "record",
    [
        None,
        {"country": {"iso_code": "GB"}},  # a country-only database has no point
        {"location": None},
        {"location": {"latitude": 51.5}},
        {"location": {"latitude": "51.5", "longitude": "0"}},
        {"location": {"latitude": True, "longitude": 0}},
        {"location": {"latitude": 91, "longitude": 0}},
        {"location": {"latitude": 0, "longitude": -181}},
    ],
)
def test_records_without_a_valid_point_are_unknown(record: object) -> None:
    assert MmdbLocationResolver(_FakeReader({"81.2.69.142": record})).locate("81.2.69.142") is None


@pytest.mark.skipif(
    not os.environ.get("PBIP_GEOIP_CITY_TEST_DB"),
    reason="set PBIP_GEOIP_CITY_TEST_DB to a DB-IP City Lite MMDB",
)
def test_real_dbip_city_database_places_well_known_addresses() -> None:
    """Manual check against the downloaded DB-IP file (no network in tests; skipped in CI)."""
    resolver = MmdbLocationResolver.open(os.environ["PBIP_GEOIP_CITY_TEST_DB"])
    london = resolver.locate("81.2.69.142")
    assert london is not None and london.country == "GB"
    assert resolver.locate("10.0.0.1") is None


def test_a_missing_or_corrupt_city_file_switches_the_rule_off_instead_of_failing(
    tmp_path: Path,
) -> None:
    """Payments must keep working: the rule only adds risk, so it degrades to location unknown."""
    location_resolver_for.cache_clear()
    corrupt = tmp_path / "corrupt.mmdb"
    corrupt.write_bytes(b"not an mmdb" * 10)
    assert isinstance(location_resolver_for(str(tmp_path / "absent.mmdb")), NoLocationResolver)
    assert isinstance(location_resolver_for(str(corrupt)), NoLocationResolver)
    location_resolver_for.cache_clear()


@pytest.mark.parametrize("scorer", ["rules", "hybrid"])
def test_both_scorers_get_the_travel_policy_from_settings(scorer: str) -> None:
    """The hybrid scorer's rules floor must apply IMPOSSIBLE_TRAVEL exactly like rules-only."""
    from pbip_api.modules.fraud.baseline import BaselineRulesScorer
    from pbip_api.modules.fraud.dependencies import build_fraud_scorer
    from pbip_api.modules.fraud.hybrid import HybridFraudScorer
    from sqlalchemy.orm import Session

    locations = MmdbLocationResolver(_FakeReader({}))
    settings = Settings(  # type: ignore[call-arg]  # pydantic-settings kwarg
        _env_file=None, fraud_scorer=scorer, fraud_travel_max_speed_kmh=700
    )
    built = build_fraud_scorer(Session(), settings, None, None, locations)
    baseline = built.baseline if isinstance(built, HybridFraudScorer) else built
    assert isinstance(baseline, BaselineRulesScorer)
    assert baseline.travel == TravelPolicy(locations, 700, 500)


def test_reason_times_are_in_utc_whatever_zone_the_database_returns() -> None:
    """The reason says "UTC": a timestamp read back in another session zone (e.g. IST) must be
    converted, not printed as local time with a UTC label (post-merge review of #365)."""
    ist = timezone(timedelta(hours=5, minutes=30))
    prior = PriorLocation("Sign-in", datetime(2026, 11, 3, 6, 12, tzinfo=ist), LONDON)
    travel = measure(prior, MUMBAI, AT)
    message = _reason(prior, MUMBAI, AT.astimezone(ist), travel).message
    assert "Sign-in from GB (London) at 00:42 UTC" in message
    assert "this payment from IN at 02:40 UTC" in message
