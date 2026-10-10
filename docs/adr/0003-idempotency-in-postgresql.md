# ADR 0003: Idempotency enforced in PostgreSQL, not Redis
- Status: Accepted
- Date: 2026-10-07
- Deciders: Architecture Team

## Context
Retries must never duplicate a transfer, even after crashes, and concurrent duplicates must collapse to one.

## Decision
`idempotency_keys(user_id, key)` is UNIQUE and inserted `ON CONFLICT DO NOTHING` in the SAME transaction as the ledger posting; losers block on the index then replay the stored response; different payload with same key -> 422.

## Alternatives considered
Redis `SET NX` - cache can lose data/expire, cannot be atomic with the ledger commit.

## Consequences
Exactly-once semantics with the business change. Redis remains for rate limiting and velocity counters.
