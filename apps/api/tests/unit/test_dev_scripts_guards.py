"""The local dev scripts that touch MFA secrets refuse staging/production before opening a DB
connection."""

from types import SimpleNamespace
from typing import NoReturn

import pytest

from scripts import seed_dev, totp

pytestmark = pytest.mark.unit


def _no_database() -> NoReturn:
    raise AssertionError("must not connect to a database")


def _deployed() -> SimpleNamespace:
    return SimpleNamespace(is_deployed=True)


def test_totp_requires_exactly_one_email() -> None:
    assert totp.main(["totp.py"]) == 2
    assert totp.main(["totp.py", "a@demo.pbip", "b@demo.pbip"]) == 2


def test_totp_refuses_a_deployed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(totp, "get_settings", _deployed)
    monkeypatch.setattr(totp, "get_engine", _no_database)

    assert totp.main(["totp.py", "analyst@demo.pbip"]) == 1


def test_seed_refuses_a_deployed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_dev, "get_settings", _deployed)
    monkeypatch.setattr(seed_dev, "get_engine", _no_database)

    assert seed_dev.main() == 1
