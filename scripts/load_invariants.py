"""Money and concurrency invariants after a k6 load run (TASK-078). Read-only.

    uv run python scripts/load_invariants.py snapshot > before.json   # before the run
    uv run python scripts/load_invariants.py check before.json        # after: exit 1 on a violation

Checks, over the whole database and the seeded load accounts (``load*@perf.pbip``):
  * no customer account is overdrawn: ledger_balance - held_amount + overdraft_limit >= 0;
  * every transaction's entries balance (debits == credits) and each account's ledger_balance equals
    the balance_after of its last entry;
  * money is conserved inside the load population (transfers only move money between seeded users);
  * PostgreSQL recorded no new deadlocks (pg_stat_database.deadlocks did not increase);
and reports the number of transfers, idempotency keys and fraud decisions created during the run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pbip_api.core.db import get_engine
from sqlalchemy import text

_QUERIES: dict[str, str] = {
    "deadlocks": "select deadlocks from pg_stat_database where datname = current_database()",
    "transfers": "select count(*) from transactions where type = 'TRANSFER'",
    "idempotency_keys": "select count(*) from idempotency_keys",
    "fraud_events": "select count(*) from fraud_events",
    "load_total_minor": """
        select coalesce(sum(a.ledger_balance), 0) from accounts a
        join account_holders h on h.account_id = a.id
        join customers c on c.id = h.customer_id
        join users u on u.id = c.user_id
        where u.email like 'load%@perf.pbip'""",
    "overdrawn_accounts": (
        "select count(*) from accounts where account_type <> 'SYSTEM' "
        "and ledger_balance - held_amount + overdraft_limit < 0"
    ),
    "unbalanced_transactions": """
        select count(*) from (
          select transaction_id from transaction_entries group by transaction_id
          having sum(case when direction = 'DEBIT' then amount else 0 end)
              <> sum(case when direction = 'CREDIT' then amount else 0 end)
        ) t""",
    "balance_mismatches": """
        select count(*) from accounts a join lateral (
          select balance_after from transaction_entries e where e.account_id = a.id
          order by e.seq desc limit 1
        ) last on true where last.balance_after <> a.ledger_balance""",
}


def snapshot() -> dict[str, int]:
    with get_engine().connect() as conn:
        return {name: int(conn.execute(text(sql)).scalar_one()) for name, sql in _QUERIES.items()}


def violations(before: dict[str, int], after: dict[str, int]) -> list[str]:
    problems = []
    for name in ("overdrawn_accounts", "unbalanced_transactions", "balance_mismatches"):
        if after[name]:
            problems.append(f"{name}: {after[name]}")
    if after["deadlocks"] > before["deadlocks"]:
        problems.append(f"deadlocks: {after['deadlocks'] - before['deadlocks']} new")
    if after["load_total_minor"] != before["load_total_minor"]:
        problems.append(
            f"money not conserved in the load population: {before['load_total_minor']} -> "
            f"{after['load_total_minor']}"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot")
    check = sub.add_parser("check")
    check.add_argument("before", type=Path)
    args = parser.parse_args(argv)
    now = snapshot()
    if args.command == "snapshot":
        sys.stdout.write(json.dumps(now, indent=2) + "\n")
        return 0
    before: dict[str, int] = json.loads(args.before.read_text(encoding="utf-8"))
    delta: dict[str, Any] = {
        k: now[k] - before.get(k, 0) for k in ("transfers", "idempotency_keys", "fraud_events")
    }
    problems = violations(before, now)
    sys.stdout.write(
        json.dumps({"after": now, "created_during_run": delta, "violations": problems}, indent=2)
        + "\n"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
