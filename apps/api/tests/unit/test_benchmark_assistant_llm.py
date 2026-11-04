"""The assistant LLM benchmark reports the load average where the OS has one (#371)."""

import os

import pytest

from scripts import benchmark_assistant_llm as bench

pytestmark = pytest.mark.unit


def test_load_is_none_without_getloadavg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no os.getloadavg (#371): the summary says None instead of crashing."""
    monkeypatch.delattr(os, "getloadavg", raising=False)
    assert bench.load_average_1m() is None


def test_load_is_none_when_the_platform_cannot_read_it(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable() -> tuple[float, float, float]:
        raise OSError("load average unobtainable")

    monkeypatch.setattr(os, "getloadavg", unreadable, raising=False)
    assert bench.load_average_1m() is None


def test_load_is_the_rounded_one_minute_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getloadavg", lambda: (1.23456, 0.5, 0.25), raising=False)
    assert bench.load_average_1m() == 1.23
