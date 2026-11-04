"""Benchmark of rules scoring with 1,000 inbound transfers (STRUCTURING F7, MULE_PASS_THROUGH F6).

Real latency is hardware-dependent, so it is measured here, outside the test suite (no CI job runs
this). ``test_scoring_cost_does_not_grow_with_1000_near_threshold_inbound_transfers`` and
``test_scoring_cost_does_not_grow_with_1000_inbound_transfers`` (test_fraud_mule.py) assert the
cost instead (the same SQL statements as for 3 credits, at most 20 rows per statement, index
scans). The script migrates a scratch database to head, opens two customers through the API's own
``AuthService``, posts ``--inbound`` transfers from one to the other through the ledger, then
scores ``--calls`` transfers from the receiver with the API's ``BaselineRulesScorer`` (every rule,
database look-ups included; no Redis, so the counters read PostgreSQL too). ``--rule structuring``
posts INR 99,500.00 credits and scores INR 5,000.00; ``--rule mule`` posts INR 100.00 credits and
scores 90% of what came in, so MULE_PASS_THROUGH fires and its repeat-episode look-up runs too. It
reports p50/p95/p99/max of the scorer's own ``latency_ms`` against AGENTS.md section 8's p95 of
100 ms, after ``--warmup`` unrecorded calls. The load average is printed before and after.

The customers and transfers stay in the scratch database (ledger rows are immutable by design).

Usage (a scratch database only: its name must start with ``pbip_test`` or ``pbip_bench``):
    uv run python scripts/benchmark_structuring_scoring.py \\
        --database-url "$BENCH_DATABASE_URL" [--rule structuring|mule] \\
        [--inbound 1000] [--calls 200] [--warmup 3] [--json results.json]
Exit code 0 on a completed run (whatever the latency), 2 if the database is not a scratch database.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_synthetic_load import environment, load_average, migrate, scratch_database_url
from measure_analytics_latency import summarise

JUST_UNDER = 9_950_000  # INR 99,500.00: inside the 90-100% band of the INR 1,00,000.00 threshold
SCORED_AMOUNT = 500_000  # INR 5,000.00, the amount the structuring test scores
MULE_INBOUND = 10_000  # INR 100.00 per credit: 1,000 of them pass the INR 10,000.00 minimum
MULE_FORWARD_PERCENT = 90  # of the inbound total, above the 80% ratio, as the mule test sends
P95_LIMIT_MS = 100.0  # AGENTS.md section 8


@dataclass(frozen=True)
class Rule:
    """What one benchmark mode posts, scores and reports."""

    code: str
    inbound_amount: int
    count_feature: str

    def scored_amount(self, inbound: int) -> int:
        if self.code == "MULE_PASS_THROUGH":
            return inbound * self.inbound_amount * MULE_FORWARD_PERCENT // 100
        return SCORED_AMOUNT


RULES = {
    "structuring": Rule("STRUCTURING", JUST_UNDER, "structuring_received_count"),
    "mule": Rule("MULE_PASS_THROUGH", MULE_INBOUND, "mule_inbound_count"),
}


def _open_customer(session: Any, settings: Any, label: str) -> tuple[uuid.UUID, uuid.UUID]:
    """(customer id, account id) of a new customer, opened the way registration opens one."""
    from pbip_api.core.crypto import FieldCipher
    from pbip_api.core.security import PasswordService
    from pbip_api.modules.accounts.service import list_accounts
    from pbip_api.modules.auth.models import RoleName
    from pbip_api.modules.auth.service import AuthService, RequestMeta
    from pbip_api.modules.customers.models import Customer
    from sqlalchemy import select

    service = AuthService(
        session,
        settings,
        PasswordService.from_settings(settings),
        FieldCipher(settings.field_encryption_key.get_secret_value()),
    )
    email = f"bench-{label}-{uuid.uuid4().hex[:8]}@example.com"
    user = service.create_user(
        email, secrets.token_urlsafe(24), "Benchmark", [RoleName.CUSTOMER], RequestMeta()
    )
    customer = session.execute(select(Customer).where(Customer.user_id == user.id)).scalar_one()
    return customer.id, list_accounts(session, customer.id)[0].id


def seed(engine: Any, settings: Any, inbound: int, rule: Rule) -> tuple[uuid.UUID, uuid.UUID]:
    """A receiver with ``inbound`` credits from another customer for ``rule``, all just posted."""
    from pbip_api.modules.ledger import service as ledger
    from pbip_api.modules.ledger.models import TransactionType
    from sqlalchemy.orm import sessionmaker

    with sessionmaker(bind=engine)() as session:
        _, sender = _open_customer(session, settings, "sender")
        customer, receiver = _open_customer(session, settings, "receiver")
        session.commit()
        ledger.deposit(session, sender, inbound * rule.inbound_amount)
        for _ in range(inbound):
            txn = ledger.create_transaction(
                session,
                type_=TransactionType.TRANSFER,
                amount=rule.inbound_amount,
                currency="INR",
                debit_account_id=sender,
                credit_account_id=receiver,
            )
            ledger.post_transaction(session, txn, ledger.transfer_legs(txn))
        session.commit()
    return customer, receiver


def measure(engine: Any, settings: Any, customer: uuid.UUID, account: uuid.UUID,
            rule: Rule, amount: int, calls: int,
            warmup: int) -> tuple[list[float], dict[str, Any]]:  # fmt: skip
    """The scorer's ``latency_ms`` per scoring, and the last result's facts for ``rule``."""
    from pbip_api.core.db import utcnow
    from pbip_api.modules.fraud.baseline import BaselineRulesScorer
    from pbip_ml.fraud.contract import TransactionContext
    from sqlalchemy.orm import sessionmaker

    fixed = utcnow() + timedelta(minutes=5)  # one clock for every call: the same window each time
    samples: list[float] = []
    facts: dict[str, Any] = {}
    for call in range(warmup + calls):
        with sessionmaker(bind=engine)() as session:
            result = BaselineRulesScorer(session, settings=settings, clock=lambda: fixed).score(
                TransactionContext(
                    transaction_id=uuid.uuid4(),
                    customer_id=customer,
                    account_id=account,
                    amount_minor=amount,
                    currency="INR",
                    occurred_at=fixed,
                )
            )
        if call >= warmup:
            samples.append(result.latency_ms)
        facts = rule_facts(rule, result.features, [r.code for r in result.reasons])
    return samples, facts


def rule_facts(rule: Rule, features: dict[str, Any], codes: list[str]) -> dict[str, Any]:
    """The counted inbound and whether the rule fired: a run that never fired measured nothing."""
    return {"rule": rule.code, "counted": features[rule.count_feature], "fired": rule.code in codes}


def benchmark_settings() -> Any:
    """Local settings with cheap password hashing (only two benchmark users are created)."""
    from cryptography.fernet import Fernet
    from pbip_api.core.config import Settings
    from pydantic import SecretStr

    return Settings(
        app_env="local",
        argon2_time_cost=1,
        argon2_memory_kib=8,
        argon2_parallelism=1,
        jwt_secret=SecretStr(secrets.token_urlsafe(48)),
        field_encryption_key=SecretStr(Fernet.generate_key().decode()),
        assistant_llm_enabled=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--rule", choices=sorted(RULES), default="structuring")
    parser.add_argument("--inbound", type=int, default=1000)
    parser.add_argument("--calls", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    try:
        url = scratch_database_url(args.database_url)
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 2

    from sqlalchemy import create_engine

    migrate(url)
    engine = create_engine(url)
    settings = benchmark_settings()
    rule = RULES[args.rule]
    amount = rule.scored_amount(args.inbound)
    customer, account = seed(engine, settings, args.inbound, rule)
    before = environment()
    samples, facts = measure(
        engine, settings, customer, account, rule, amount, args.calls, args.warmup
    )
    stats = summarise(samples)
    result = {
        "inbound": args.inbound,
        "scored_amount_minor": amount,
        **facts,
        **stats,
        "p95_limit_ms": P95_LIMIT_MS,
        "p95_within_limit": float(stats["p95_ms"]) < P95_LIMIT_MS,
        "environment_before": before,
        "load_average_after": load_average(),
    }
    engine.dispose()
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
