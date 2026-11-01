# ADR 0020: Device and network fraud signals, and Redis velocity counters
- Status: Accepted
- Date: 2026-11-01
- Deciders: Architecture Team

## Context
TASK-095 adds the UC-09 triggers "multiple login failures" and "new location", read as a new device or
network. TASK-034 adds Redis sliding-window counters. Until now, the payment path passed the fraud scorer
neither a device nor any part of the client IP (only its country, ADR-0014). `TransactionContext` is a
cross-domain contract (AGENTS.md §2), so changing it needs this record.

## Decision
1. **Contract, additive only.** `TransactionContext.ip_network: str | None`, the client IP truncated to /24
   (IPv4) or /48 (IPv6). The full address never enters the contract, the snapshot or the reasons. The existing
   `device_id: UUID | None` gets used.
2. **Device id.** The web client already sends a random per-browser `X-Device-ID` (`lib/device.ts`). Transfers
   and bill payments read that header (optional, at most 512 characters) and turn it into
   `uuid5(namespace, sha256(raw))`. That is the same SHA-256 the device registry stores, so a registered
   device is recognised. The raw value is never stored or logged.
3. **Rules** (baseline `baseline-rules-0.3`):
   - `RECENT_LOGIN_FAILURES` (+20): 3 or more `LOGIN_FAILURE` security events on the customer's login in
     24 hours.
   - `NEW_DEVICE_OR_NETWORK` (+15): the device, or the network, never appeared on this customer's scored
     transfers in 180 days. Registered devices count as seen. As with `NEW_COUNTRY`, no history or no
     device/network never adds risk.

   Each signal alone stays below STEP_UP (30). Together they reach it: the account-takeover pattern.
4. **Velocity counters.** Redis sorted sets, scored by epoch seconds, give true sliding windows:
   - per account: transfers in 10 minutes, and volume in 1 hour;
   - per IP: failed sign-ins in 24 hours.

   Every write is one `MULTI/EXEC` pipeline: add, trim the expired tail, refresh the TTL. Writes happen
   **after** the payment commits, so scoring stays side-effect free and a rolled-back attempt is never
   counted. Reads that fail (Redis down) return `None`, and the scorer counts from PostgreSQL instead.
   Redis is a cache of facts the ledger and `security_events` already hold.

## Alternatives considered
- **Pass the full IP in the contract.** Rejected: personal data in every fraud snapshot, for no gain. A /24
  already tells "usual network" from "new network".
- **A browser fingerprint (canvas, fonts).** Rejected: invasive, and it needs consent. The random id is
  enough to recognise a returning browser.
- **Fixed-window counters (INCR + EXPIRE)** like the rate limiter. Rejected: a burst straddling two windows
  is split in half, which defeats a velocity rule.

## Consequences
- A customer who clears site data gets a new device id. That is +15 points, below STEP_UP on its own.
- Redis emptied (restart, flush) under-counts velocity for up to 10 minutes. Unreachable Redis does not,
  because PostgreSQL answers then.
- The IP prefix is personal data with a 180-day lookback in `fraud_events`. It is covered by the same
  retention as the rest of the fraud snapshot.
