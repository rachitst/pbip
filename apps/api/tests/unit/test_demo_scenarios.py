"""The live demo script (demo/demo_scenarios.py) must still show what it promises on a demo DB
that is days old: the fraud-new-payee scenario has to send to a payee created on THIS run.

Regression (TASK-089 bug bash): the scenario reused the "Bob Demo" payee, so once that payee was
older than the new-payee window the large transfer posted instead of being held for review.
The HTTP layer is replaced by a recorder; nothing here talks to a running API.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]


def _load_demo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_scenarios", ROOT / "demo/demo_scenarios.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeApi:
    """Answers the calls fraud_new_payee makes and records every request body."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.created = 0
        self.accounts_with_a_payee = {"000000000002"}  # Bob's account: "Bob Demo" exists already

    def __call__(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        self.requests.append((method, path, body))
        if path == "/api/v1/auth/login":
            return 200, {}, {"access_token": f"{body['email'].split('@')[0]}-token"}
        if method == "GET" and path == "/api/v1/accounts":
            own = "000000000002" if token == "bob-token" else "000000000001"
            return 200, {}, [{"id": f"acc-{own}", "account_number": own}]
        if method == "GET" and path == "/api/v1/payees":
            return 200, {}, [{"id": "old-bob-payee", "name": "Bob Demo"}]
        if method == "POST" and path == "/api/v1/payees":
            if body["account_number"] in self.accounts_with_a_payee:
                return 409, {}, {"title": "Payee already exists"}
            self.accounts_with_a_payee.add(body["account_number"])
            self.created += 1
            return 201, {}, {"id": f"new-payee-{self.created}"}
        if path == "/api/v1/transfers":
            return 202, {}, {"status": "HELD", "message": "under review"}
        return 200, {}, {}

    def transfer_payees(self) -> list[str]:
        return [body["payee_id"] for _, path, body in self.requests if path == "/api/v1/transfers"]


def test_fraud_new_payee_sends_to_a_payee_created_on_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo = _load_demo()
    api = _FakeApi()
    monkeypatch.setattr(demo, "call", api)

    demo.fraud_new_payee()
    demo.fraud_new_payee()

    # Each run sends to the payee it has just created, never to an older one.
    assert api.transfer_payees() == ["new-payee-1", "new-payee-2"]
    assert "old-bob-payee" not in api.transfer_payees()
