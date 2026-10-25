import base64
import contextlib
import json
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pbip_api.core.errors import UnprocessableError
from pbip_api.core.pagination import MAX_CURSOR_LENGTH, Cursor, decode_cursor, encode_cursor

pytestmark = pytest.mark.unit

# Hypothesis requires naive bounds when `timezones` is given; the generated values are UTC-aware.
aware_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),  # noqa: DTZ001
    max_value=datetime(2100, 1, 1),  # noqa: DTZ001
    timezones=st.just(UTC),
)


@given(seq=st.integers(min_value=1, max_value=2**63 - 1), ts=aware_datetimes)
def test_cursor_round_trips_seq_and_timestamp(seq: int, ts: datetime) -> None:
    raw = encode_cursor(seq, ts)
    assert len(raw) <= MAX_CURSOR_LENGTH
    assert decode_cursor(raw) == Cursor(seq=seq, ts=ts)


@given(seq=st.integers(min_value=1, max_value=2**63 - 1))
def test_legacy_integer_cursors_are_still_accepted(seq: int) -> None:
    assert decode_cursor(str(seq)) == Cursor(seq=seq)


def _b64(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "0",
        str(2**63),  # legacy form beyond BIGINT: would overflow in SQL (was a 500)
        "9" * 30,
        _b64({"v": 1, "seq": 2**63, "ts": "2026-01-01T00:00:00+00:00"}),
        "x" * (MAX_CURSOR_LENGTH + 1),
        "not base64 !!",
        _b64([1, 2]),
        _b64({"v": 1, "seq": 5}),
        _b64({"v": 2, "seq": 5, "ts": "2026-01-01T00:00:00+00:00"}),
        _b64({"v": 1, "seq": 0, "ts": "2026-01-01T00:00:00+00:00"}),
        _b64({"v": 1, "seq": "5", "ts": "2026-01-01T00:00:00+00:00"}),
        _b64({"v": 1, "seq": True, "ts": "2026-01-01T00:00:00+00:00"}),
        _b64({"v": 1, "seq": 5, "ts": "2026-01-01T00:00:00"}),  # naive timestamp
        _b64({"v": 1, "seq": 5, "ts": "yesterday"}),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
    ],
)
def test_malformed_cursors_are_a_422_not_a_500(raw: str) -> None:
    with pytest.raises(UnprocessableError):
        decode_cursor(raw)


@given(st.text(max_size=MAX_CURSOR_LENGTH))
def test_decoding_arbitrary_text_never_raises_anything_but_422(raw: str) -> None:
    with contextlib.suppress(UnprocessableError):
        decode_cursor(raw)
