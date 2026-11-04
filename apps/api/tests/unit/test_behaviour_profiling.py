"""Session derivation, engagement and product-interest scoring (UC-11, ADR-0057).

Every expected number is worked out by hand from the rules in ``profiling``'s docstring.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pbip_api.modules.analytics.profiling import (
    INACTIVITY_TIMEOUT,
    PRODUCT_PAGES,
    ProfileEvent,
    derive_sessions,
    interest_signal,
    score_interests,
    summarise_engagement,
)
from pbip_ml.recommendations import ProductType
from pbip_ml.recommendations.engine import CREDIT_LINE_ID, FAMILY_COVER_ID, INDEX_SIP_ID

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def ev(
    at: datetime,
    kind: str = "page_view",
    session: str = "s1",
    page: str | None = None,
    product: str | None = None,
) -> ProfileEvent:
    return ProfileEvent(session, kind, at, page, product)


def test_no_events_gives_no_sessions_and_zero_engagement() -> None:
    assert derive_sessions([]) == []
    e = summarise_engagement([])
    assert (e.sessions, e.avg_session_seconds, e.pages_per_session, e.last_active_at) == (
        0,
        0,
        0.0,
        None,
    )


def test_a_single_event_is_a_session_of_zero_seconds() -> None:
    [only] = derive_sessions([ev(T0)])
    assert (only.start, only.end, only.seconds, only.interactions, only.page_views) == (
        T0,
        T0,
        0,
        1,
        1,
    )


def test_duration_is_last_minus_first_event() -> None:
    [s] = derive_sessions([ev(T0), ev(T0 + timedelta(seconds=75), "account_viewed")])
    assert s.seconds == 75 and s.interactions == 2 and s.page_views == 1


def test_a_gap_longer_than_the_timeout_starts_a_new_session() -> None:
    later = T0 + timedelta(seconds=40) + INACTIVITY_TIMEOUT + timedelta(seconds=1)
    sessions = derive_sessions([ev(T0), ev(T0 + timedelta(seconds=40)), ev(later)])
    assert [(s.start, s.seconds) for s in sessions] == [(T0, 40), (later, 0)]


def test_a_gap_of_exactly_the_timeout_keeps_the_session() -> None:
    [s] = derive_sessions([ev(T0), ev(T0 + INACTIVITY_TIMEOUT)])
    assert s.seconds == INACTIVITY_TIMEOUT.total_seconds()


def test_out_of_order_events_are_sorted_before_splitting() -> None:
    arrived = [
        ev(T0 + timedelta(minutes=50)),
        ev(T0 + timedelta(seconds=30)),
        ev(T0),
        ev(T0 + timedelta(minutes=45)),
    ]
    sessions = derive_sessions(arrived)
    assert [(s.start, s.seconds) for s in sessions] == [
        (T0, 30),
        (T0 + timedelta(minutes=45), 300),
    ]


def test_session_ids_are_never_merged_and_sessions_come_oldest_first() -> None:
    sessions = derive_sessions([ev(T0 + timedelta(seconds=5), session="b"), ev(T0, session="a")])
    assert [(s.session_id, s.start) for s in sessions] == [
        ("a", T0),
        ("b", T0 + timedelta(seconds=5)),
    ]


def test_heartbeats_extend_a_session_but_are_not_interactions() -> None:
    beat = "session_heartbeat"
    [s] = derive_sessions([ev(T0), ev(T0 + timedelta(seconds=60), beat), ev(T0 + timedelta(seconds=120), beat)])  # fmt: skip
    assert (s.seconds, s.interactions, s.page_views) == (120, 1, 1)


def test_a_run_of_heartbeats_alone_is_not_a_session() -> None:
    beat = "session_heartbeat"
    late = T0 + timedelta(hours=2)
    sessions = derive_sessions([ev(T0), ev(late, beat), ev(late + timedelta(seconds=60), beat)])
    assert [s.start for s in sessions] == [T0]


def test_engagement_summary() -> None:
    sessions = derive_sessions(
        [
            ev(T0),
            ev(T0 + timedelta(seconds=100), "product_clicked"),
            ev(T0 + timedelta(hours=3)),
            ev(T0 + timedelta(hours=3, seconds=1)),
            ev(T0 + timedelta(days=1), "account_viewed"),
        ]
    )
    e = summarise_engagement(sessions)
    assert (e.sessions, e.avg_session_seconds, e.page_views, e.interactions) == (3, 34, 3, 5)
    assert e.pages_per_session == 1.0  # 3 page views / 3 sessions
    assert e.last_active_at == T0 + timedelta(days=1)


def test_interest_signals_and_their_weights() -> None:
    assert interest_signal(ev(T0, "product_clicked", product=str(CREDIT_LINE_ID))) == (
        ProductType.LOAN,
        3.0,
    )
    assert interest_signal(ev(T0, "product_clicked", product=str(FAMILY_COVER_ID))) == (
        ProductType.INSURANCE,
        3.0,
    )
    assert interest_signal(ev(T0, "loan_calculator_used")) == (ProductType.LOAN, 2.0)
    assert interest_signal(ev(T0, page="/loans")) == (ProductType.LOAN, 1.0)
    assert interest_signal(ev(T0, page="/wealth/compare")) == (ProductType.INVESTMENT, 1.0)
    assert set(PRODUCT_PAGES) == {"/loans", "/wealth", "/wealth/compare"}


@pytest.mark.parametrize(
    "event",
    [
        ev(T0, "product_clicked", product="00000000-0000-4000-a000-0000000000ff"),
        ev(T0, "product_clicked", product=None),
        ev(T0, page="/accounts"),
        ev(T0, page=None),
        ev(T0, "account_viewed", page="/loans"),  # only a page VIEW of a product page counts
        ev(T0, "session_heartbeat", page="/loans"),
        ev(T0, "transfer_initiated"),
    ],
)
def test_events_that_say_nothing_about_products_are_not_signals(event: ProfileEvent) -> None:
    assert interest_signal(event) is None


def test_a_signal_loses_half_its_weight_every_fourteen_days() -> None:
    now = T0 + timedelta(days=28)
    [loan] = score_interests([ev(T0, "loan_calculator_used")], now)
    assert loan.score == 0.5  # 2.0 * 0.5 ** (28 / 14)
    assert (loan.category, loan.signals, loan.last_signal_at) == (ProductType.LOAN, 1, T0)


def test_scores_sum_per_category_and_sort_highest_first() -> None:
    now = T0
    interests = score_interests(
        [
            ev(T0, page="/loans"),
            ev(T0 - timedelta(days=14), "loan_calculator_used"),
            ev(T0, "product_clicked", product=str(INDEX_SIP_ID)),
            ev(T0 - timedelta(days=14), page="/wealth"),
        ],
        now,
    )
    assert [(i.category, i.score, i.signals) for i in interests] == [
        (ProductType.INVESTMENT, 3.5, 2),  # 3 + 1 * 0.5
        (ProductType.LOAN, 2.0, 2),  # 1 + 2 * 0.5
    ]
    assert interests[0].last_signal_at == T0


def test_equal_scores_are_ordered_by_category_name() -> None:
    interests = score_interests([ev(T0, page="/wealth"), ev(T0, page="/loans")], T0)
    assert [i.category for i in interests] == [ProductType.INVESTMENT, ProductType.LOAN]


def test_signals_outside_the_window_are_ignored_and_future_ones_count_as_now() -> None:
    now = T0
    interests = score_interests(
        [
            ev(T0 - timedelta(days=91), "loan_calculator_used"),
            ev(T0 + timedelta(hours=5), page="/wealth"),
        ],
        now,
    )
    assert [(i.category, i.score) for i in interests] == [(ProductType.INVESTMENT, 1.0)]
