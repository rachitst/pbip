# ADR 0054: Version-checked in-process read caches for the recommendation context and the forecast fit
- Status: Accepted (under owner's standing approval, 2026-11-04)
- Date: 2026-11-04
- Deciders: Architecture Team (issues #343 and #175)

## Context
Two customer read paths spend most of their time recomputing results whose inputs rarely change:

- `GET /api/v1/recommendations` (#343, target p95 < 40 ms). After #395/#416 a request is 7 SQL statements: token
  check, customer, accounts, one pass over all of the customer's ledger entries (health window and lifestyle
  features), segment, loans, fixed deposits. On the 2-CPU reference laptop it used 15 ms of server CPU against 8.7 ms
  for `GET /api/v1/accounts` (3 statements), and its HTTP p95 was 114-121 ms against 58-70 ms for accounts at a load
  average of about 8.
- `GET /api/v1/analytics/forecast` (#175, analytics < 50 ms). Every request fits a statsforecast AutoARIMA model on
  the account's balance history (`pbip_ml.forecasting.model`, "about 0.9 s per fit"): p50 1.6-2.0 s for a customer
  with three months of history on the laptop. The fit's inputs are the balance history up to yesterday, the first
  forecast day and the horizon, so they change once a day.

Issue #343 asked for caching with explicit invalidation on the events that change the context (ledger posting,
consent, profile change), keyed by the authenticated customer, never serving stale consent; Redis is available. A
cache is an architectural change (AGENTS.md rule 5).

Options considered:

1. **Redis cache plus explicit invalidation** (delete or bump a per-customer generation after commit in every write
   path: ledger postings and holds, account open/close, loan and deposit state, segmentation job, consent). Shared by
   both API replicas. But the write paths are many and spread over modules, `ledger/service.py` does not commit (its
   callers do), so a post-commit hook is needed to avoid caching data read before the commit; a lost invalidation
   (Redis briefly down, a new write path that forgets the call) serves stale data until a TTL. Financial aggregates
   would also sit in Redis.
2. **Reuse stored monthly analytics** (`monthly_category_spending`). It holds per-category debits of closed months
   only. The recommendation context also needs recurring income, the running month, own-account exclusions and
   lifetime lifestyle features, so it cannot be built from it without changing the figures customers see.
3. **One combined SQL statement** across accounts, entries, segment, loans and deposits. It would read four modules'
   tables from one repository, breaking the module boundaries (AGENTS.md section 2), and would still scan the
   lifetime ledger on every request.
4. **Version-checked in-process cache** (chosen).

## Decision
1. `core/versioned_cache.py`: `VersionedCache`, a bounded LRU in the API process, thread-safe, with a TTL. An entry is
   stored under a key AND a version, and a lookup returns it only for the same key and an equal version; any other
   version drops it. The TTL only bounds memory. Lookups are counted in `pbip_read_cache_lookups_total{cache,
   outcome}` (fixed cache names, no keys).
2. **Recommendations.** The accounts policy gains `list_owned_accounts_with_versions`: the same single query as
   `list_owned_accounts`, plus each row's PostgreSQL `xmin`, the id of the transaction that wrote that row version.
   The key is the authenticated customer id. The version is the UTC date, the bank date, and for every owned account
   its id, `xmin`, ledger balance, held amount and status. Cached: health score, monthly income and spend, savings,
   discretionary, buffer and EMI ratios, and the held loans and deposits. Read live on every request: the segment,
   the balances and held accounts, and the age band (date of birth AND consent, from the customer row authentication
   already loaded, ADR-0015).
3. **Forecast.** The fit result (method and model days) is cached under the key (customer id, account id) and a
   SHA-256 of the model version, first forecast day, horizon and the exact input series. It is memoisation of a pure
   function: no invalidation exists because a different input is a different version. Today's balance (the anchor),
   scheduled obligations and the low-balance alert are still computed on every request.
4. Bounds: 10,000 recommendation entries (about 1 KB each, TTL 15 min) and 2,000 forecast entries (a few KB each,
   TTL 6 h) per API process.

### Why the version is enough
Every input of the cached recommendation figures either changes an owned account's row or is in the version:

- The ledger is the only writer of balances and holds (AGENTS.md section 4). Every posting updates the balance of
  each account it touches, and every hold changes `held_amount`, so each writes a new row version with a new `xmin`,
  including money received from another customer and a posting that returns the balance to an earlier value.
  A plain read or a `FOR UPDATE` lock does not change `xmin` (tested).
- Loans and fixed deposits change state together with a ledger posting on the customer's account (disbursal, EMI
  collection and the closure it triggers, booking, premature closure, maturity payout).
- A new or closed account changes the set of ids or a status; joint holders see each other's postings through the
  shared row.
- Time: health and cash flow are per UTC calendar month, deposit maturity per bank day; both dates are in the version.
- The ledger entries themselves are immutable (DB trigger), so nothing rewrites the history under an unchanged row.

A reader that reads the accounts and then the entries under READ COMMITTED can only see entries at least as new as
the versions it read, so a stored entry is never older than its version; at worst it is newer, which is harmless.

## Consequences
- Warm recommendation request: 4 statements instead of 7 (token check, customer, accounts with versions, segment).
  Server CPU per request falls from 13.3-13.6 ms to 7.4-8.1 ms, about what `GET /api/v1/accounts` costs (6.6-8.3 ms).
  HTTP p95 is 26-36 ms against 58-61 ms on main, at a load average below 4 on the laptop
  (`docs/ml/recommendation-propensity-decision.md`). A cold request is unchanged (7 statements). Repeat forecast: no
  model fit, p95 37-44 ms instead of 1.0-1.2 s (`docs/testing/analytics-latency-2026-11-04.md`).
- No explicit invalidation calls, no post-commit hooks, no Redis dependency on these paths, and no cross-replica
  invalidation: each replica checks the version itself. Each replica warms its own cache, so with two replicas a
  customer's first request on each one is cold.
- Consent, date of birth and segment changes are visible on the next request (never cached).
- The cache is per process memory: tens of MB at the bounds above. A restart empties it.
- `xmin` is PostgreSQL-specific; the project uses PostgreSQL only (tests included). Transaction-id wraparound cannot
  produce a false hit within the 15-minute TTL.
- A future write that changes loans or deposits WITHOUT touching an owned account would be served from the cache for
  up to the TTL. Such a change must post through the ledger (it does today) or add its own version component; this ADR
  and the module docstring say so.

## Security
- Keys are the authenticated customer id (from `get_current_customer`, never from the request); a test proves one
  customer is never served another's entry. Staff RM views call the same service with the customer id they are
  already authorised for.
- No data leaves the process; nothing is written to Redis or logs. Threat model T68.

## Alternatives rejected
Options 1-3 above. Redis with a version check instead of in-process memory would share entries between replicas but
adds a round trip and a failure mode to every request and puts financial aggregates in Redis; it can be revisited if
the replica count grows.
