# ADR 0008: Ledger pagination keyset is the entry `seq`; the cursor also carries its timestamp
- Status: Accepted
- Date: 2026-10-25
- Deciders: Architecture Team

## Context
TASK-008 requires cursor pagination of `GET /api/v1/accounts/{id}/transactions` that is stable
under concurrent writes, with a cursor that encodes the entry timestamp and sequence id.
`transaction_entries.created_at` is `now()`, i.e. the *start* time of the posting DB transaction.
Postings to one account are serialised by that account's `FOR UPDATE` row lock, so a transaction
that started earlier can commit later: `created_at` is not monotonic in commit order, while the
identity column `seq` (assigned at insert, under the lock) is.

## Decision
Order and filter by `seq` only (`seq < cursor` walking older, `seq > cursor` walking newer).
The cursor is an opaque, versioned base64url JSON `{v, seq, ts}`: `ts` tells clients and operators
*when* a page position is, but never drives the query. Bare integer cursors from the previous
format remain accepted. Ownership is checked before the cursor is decoded, so a foreign account
is 404 whatever the cursor says.

## Alternatives considered
- `(created_at, seq)` keyset: a late-committing posting with an earlier `created_at` lands behind
  pages a client already read, and the order would contradict `balance_after`.
- Offset pagination: drifts (duplicates/skips) whenever entries are posted between page requests.
- Signed (HMAC) cursors: unnecessary - a cursor grants nothing beyond the authorized account.

## Consequences
Walking older is exactly the snapshot at the first page; walking newer picks up new postings once
each (property-tested with Hypothesis plus a real-thread writer). If entries are ever written
without the account lock (e.g. sharded clearing), this ADR must be revisited.
