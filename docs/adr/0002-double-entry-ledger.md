# ADR 0002: Double-entry ledger with immutable entries
- Status: Accepted
- Date: 2026-10-07
- Deciders: A, B

## Context
A mutable `balance` column plus source/destination columns cannot explain history, prove consistency, or support reversals and audit.

## Decision
Transactions are instructions; `transaction_entries` are immutable balanced lines; `accounts.ledger_balance` is a materialised running balance updated under row locks in the same DB transaction. DB triggers enforce immutability and balance; a CHECK constraint forbids overdraw.

## Alternatives considered
Mutable balance only - no audit trail; full event sourcing - overkill and slower to build.

## Consequences
Strong, testable invariants and clear viva story. Cost: more code, a hot clearing account, strict lock ordering (documented in AGENTS.md).
