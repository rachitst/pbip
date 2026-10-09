"""Live demo scenarios against a running API (stdlib only, no extra installs).

    uv run python demo/demo_scenarios.py idempotency
    uv run python demo/demo_scenarios.py fraud-new-payee
    uv run python demo/demo_scenarios.py bola

Prerequisite: `make seed` (demo users) and the API running (make dev-api / make up).
"""

import json
import sys
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

BASE = "http://localhost:8000"
PASSWORD = "Demo-Password-2026!"  # noqa: S105 - local demo data only


def call(
    method: str,
    path: str,
    token: str | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    request = urllib.request.Request(  # noqa: S310
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            raw = response.read()
            return (
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                json.loads(raw) if raw else None,
            )
    except urllib.error.HTTPError as error:
        raw = error.read()
        return (
            error.code,
            {k.lower(): v for k, v in error.headers.items()},
            json.loads(raw) if raw else None,
        )


def login(email: str) -> str:
    status, _, body = call(
        "POST", "/api/v1/auth/login", body={"email": email, "password": PASSWORD}
    )
    assert status == 200, body
    return str(body["access_token"])


def account(token: str) -> dict[str, Any]:
    return dict(call("GET", "/api/v1/accounts", token)[2][0])


def verified_payee(token: str, target_number: str, name: str) -> str:
    status, _, created = call(
        "POST", "/api/v1/payees", token, {"name": name, "account_number": target_number}
    )
    if status == 409:
        existing = [p for p in call("GET", "/api/v1/payees", token)[2] if p["name"] == name]
        payee_id = existing[0]["id"]
    else:
        payee_id = created["id"]
    call("POST", f"/api/v1/payees/{payee_id}/verify", token)
    return str(payee_id)


def show(title: str) -> None:
    sys.stdout.write(f"\n=== {title} ===\n")


def idempotency() -> None:
    alice, bob = login("alice@demo.pbip"), login("bob@demo.pbip")
    src, dst = account(alice), account(bob)
    payee = verified_payee(alice, dst["account_number"], "Bob Demo")
    key = f"demo-{uuid.uuid4().hex}"
    body = {
        "from_account_id": src["id"],
        "amount_minor": 10_000,
        "payee_id": payee,
        "reference": "idempotency demo",
    }
    show(f"Alice balance before: {src['ledger_balance_minor']}")
    show("Firing 10 IDENTICAL requests at once (same Idempotency-Key)")
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(
            pool.map(
                lambda _: call("POST", "/api/v1/transfers", alice, body, {"Idempotency-Key": key}),
                range(10),
            )
        )
    for status, headers, payload in results:
        replayed = headers.get("idempotent-replayed", "false")
        sys.stdout.write(f"  HTTP {status}  transfer={payload['id'][:8]}  replayed={replayed}\n")
    after = account(alice)["ledger_balance_minor"]
    ids = {payload["id"] for _, _, payload in results}
    sys.stdout.write(
        f"\nDistinct transfers created: {len(ids)}   "
        f"Alice balance after: {after} (moved {src['ledger_balance_minor'] - after})\n"
    )
    assert len(ids) == 1, "duplicate transfers were created!"


def fraud_new_payee() -> None:
    alice, bob = login("alice@demo.pbip"), login("bob@demo.pbip")
    src, dst = account(alice), account(bob)
    status, _, created = call(
        "POST",
        "/api/v1/payees",
        alice,
        {"name": "Bob Demo", "account_number": dst["account_number"]},
    )
    payee = (
        created["id"]
        if status == 201
        else next(p["id"] for p in call("GET", "/api/v1/payees", alice)[2])
    )
    call("POST", f"/api/v1/payees/{payee}/verify", alice)
    show("Large transfer to a brand-new payee (>= 25,000.00)")
    status, _, payload = call(
        "POST",
        "/api/v1/transfers",
        alice,
        {"from_account_id": src["id"], "amount_minor": 3_000_000, "payee_id": payee},
        {"Idempotency-Key": f"demo-{uuid.uuid4().hex}"},
    )
    sys.stdout.write(
        f"  HTTP {status}  status={payload['status']}  message={payload['message']!r}\n"
    )
    sys.stdout.write("  Open the analyst view / GET /api/v1/fraud/alerts to see WHY it was held.\n")


def bola() -> None:
    alice, bob = login("alice@demo.pbip"), login("bob@demo.pbip")
    alice_account = account(alice)
    show("Bob tries to read Alice's account by changing the id in the URL")
    status, _, payload = call("GET", f"/api/v1/accounts/{alice_account['id']}", bob)
    sys.stdout.write(
        f"  HTTP {status}  {payload['title']!r}   (404 - indistinguishable from 'does not exist')\n"
    )
    assert status == 404


SCENARIOS = {"idempotency": idempotency, "fraud-new-payee": fraud_new_payee, "bola": bola}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        sys.stderr.write(f"usage: demo_scenarios.py {{{'|'.join(SCENARIOS)}}}\n")
        raise SystemExit(2)
    SCENARIOS[sys.argv[1]]()
