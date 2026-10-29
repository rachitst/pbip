# ADR 0014: Country-level GeoIP from DB-IP Lite for the NEW_COUNTRY fraud signal
- Status: Accepted
- Date: 2026-10-30
- Deciders: Architecture Team (product owner decision to implement "new geographic location", UC-09)

## Context
UC-09 lists "new geographic location" as a fraud trigger. The fraud contract already carries
`TransactionContext.ip_country`, but nothing filled it, and PBIP had no IP geolocation data. The
request IP reaches the API through nginx (`X-Forwarded-For`, trusted by uvicorn's
`--proxy-headers`).

## Options considered
| Source | Licence and access | Updates | Accuracy information | Verdict |
|---|---|---|---|---|
| **DB-IP "IP to Country Lite"** | CC BY 4.0; direct download, no account; attribution "IP Geolocation by DB-IP" | Monthly; CSV and MMDB (about 4 MB gzipped, 8 MB MMDB) | DB-IP's own "Accuracy Index" is 81 for Lite vs 93 for its paid country product (an internal vendor measure, not a percentage) | **Chosen** |
| MaxMind GeoLite2 Country | GeoLite EULA; account and licence key required; copies must be deleted within 30 days of a new release | Regular releases | MaxMind states IP geolocation "is inherently imprecise"; no country accuracy figure on the product page | Rejected: account and key management, stricter deletion duty |
| Commercial APIs (per-request lookup) | Paid, network call on the payment path | Live | Vendor-specific | Rejected: external call during scoring, cost, sends customer IPs to a third party |

Facts above were checked on the vendors' pages on 2026-10-30. The reader is `maxminddb` 3.2.0
(Apache-2.0, no dependencies, MaxMind's MMDB reader, works with DB-IP's MMDB files).

## Decision
* `core/geoip.py` resolves an IP to an ISO country code or `None`. Private, loopback, link-local,
  reserved and malformed addresses are never looked up. With no database configured
  (`GEOIP_COUNTRY_DB_PATH` empty) every answer is `None`.
* The payments service fills `ip_country`; the baseline scorer (`baseline-rules-0.2`) records it in
  the fraud feature snapshot and adds **NEW_COUNTRY (+20)** when the country differs from every
  country in that customer's scored transfers of the last 180 days. 20 is below the step-up
  threshold (30): a new country alone is a signal shown to analysts, and only escalates combined
  with another rule (e.g. new payee, amount spike).
* No baseline (first located transfer) and unknown country never add risk.
* The database is downloaded monthly by `scripts/fetch_geoip.py` (verifies before replacing) and
  mounted read-only; it is not committed (size, monthly freshness).

## Accuracy, privacy and operations
* **Accuracy:** country-level only, approximate. Spot check of the 2026-10 file: 8.8.8.8 -> US,
  81.2.69.142 -> GB, 49.36.0.1 -> IN, but anycast addresses land wherever the operator registered
  them (1.1.1.1 -> AU, Google's IPv6 DNS -> CA). VPNs, mobile carriers and cloud egress are
  misplaced routinely. **This rule is not a reliable location detector and must not decide alone**;
  no detection-rate claim is made because no labelled evaluation exists (TASK-023/029 synthetic
  data could provide one).
* **Privacy:** only the two-letter code is stored, in the fraud feature snapshot (staff-only). The
  IP itself was already recorded in security events; no new personal data category is collected.
  The customer response never shows reasons (existing rule).
* **Operations:** monthly refresh (runbook 2.4); a stale file degrades accuracy slowly; a missing or
  unreadable file disables the rule instead of failing transfers. Attribution is required wherever
  location results are displayed.
* **Spoofing:** forwarded headers are trusted only because the API is reachable solely through
  nginx; exposing port 8000 would let a client forge its country (threat model T31). A VPN in the
  victim's country evades the rule; it only ever adds risk.

## Consequences
TASK-095's geographic criterion is implemented; its login-failure and new-device criteria remain
open. Re-evaluate the points value once labelled data exists.
