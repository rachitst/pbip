# ADR 0034: DB-IP City Lite as the location source for the IMPOSSIBLE_TRAVEL fraud rule
- Status: Accepted (under owner's standing approval, 2026-11-03). Merged in #365 before review, under a WIP
  commit title (see "Review after merge" below); reviewed after merge and completed in the follow-up PR.
- Date: 2026-11-03
- Deciders: Architecture Team

## Context
Blueprint §8 scenario F4 ("impossible travel / new geography", tier MUST) and issue #143 ask for a rule that
flags a payment when the customer's previous event and this one are too far apart for the time between them.
ADR-0014 added country-level GeoIP (DB-IP "IP to Country Lite") and NEW_COUNTRY, but a country code has no
position, so no distance can be computed from it. `TransactionContext.geo_lat`/`geo_lon` exist in the fraud
contract and were never filled. Sign-ins and token refreshes keep the client IP in `security_events.ip`.

## Options considered
| Source | Distance it gives | Licence and access | Verdict |
|---|---|---|---|
| **DB-IP "IP to City Lite"** (MMDB) | Point (latitude/longitude) of the city DB-IP assigns to the address block | CC BY 4.0, direct download, no account, attribution "IP Geolocation by DB-IP"; same vendor, licence, download script and reader (`maxminddb`) as ADR-0014 | **Chosen** |
| Country-level, with a committed table of one reference point per country (e.g. Natural Earth label points, public domain) | Distance between two country reference points | Small and committed; no new download | Rejected: it is **not a lower bound**. Two places a few km apart across a border (neighbouring countries) get the full centroid-to-centroid distance (false positives); two cities thousands of km apart in one country get 0 (false negatives). A true lower bound would need country polygons and is 0 for every pair of neighbours, so it would only ever fire for non-neighbouring countries |
| MaxMind GeoLite2 City | Point, plus an accuracy radius | GeoLite EULA, account and licence key, deletion duty within 30 days of a new release | Rejected for the same reasons as in ADR-0014 |
| Commercial per-request geolocation APIs | Point | Paid; a network call on the payment path; sends customer IPs to a third party | Rejected |
| Coordinates from the data generator only | Synthetic points | n/a | Rejected as the source: real requests carry no coordinates. Synthetic F4 rows today carry a country code and `city="(abroad)"` only |

Facts about City Lite, from DB-IP's download page and the file itself, checked on 2026-11-03: CC BY 4.0;
"updated monthly"; MMDB 121.0 MB with 7,777,816 records (the November 2026 `.mmdb.gz` download is 60,125,343
bytes); the page describes Lite as "a subset ... with reduced accuracy and number of records" and lists an
"Accuracy Index" of 77 for City Lite against 96 for the commercial IP to Location product. That index is a vendor
internal measure, not a percentage or a distance error, and PBIP has no measurement of its own. Spot checks of
the November file: 81.2.69.142 -> London GB, 8.8.8.8 -> Mountain View US, 49.36.0.1 (a national mobile carrier's
block) -> Mangaluru IN. Lookups took about 9 microseconds each on the reference laptop.

## Decision
1. **Source:** DB-IP City Lite, read by `core/geoip.MmdbLocationResolver` (`GEOIP_CITY_DB_PATH`), downloaded and
   verified by `scripts/fetch_geoip.py --product city` (it checks that two known addresses resolve to the
   expected country and have a point before replacing the file), mounted read-only, not committed. Private,
   loopback, link-local, reserved and malformed addresses are never looked up. No lookup ever leaves the process.
2. **Current point:** the payments and bills services fill `TransactionContext.geo_lat`/`geo_lon` from the
   request IP. When no country file is configured, `ip_country` is taken from the same city record (it carries
   the ISO code), so one City Lite file can serve both NEW_COUNTRY and this rule; a configured country file still
   takes precedence. Scheduled transfers run without a request IP, so their location is unknown. The fraud
   contract is unchanged (the fields already existed).
3. **Previous point:** the customer's most recent *located* event before this payment, among
   * sign-ins (`LOGIN_SUCCESS`) and token refreshes (`TOKEN_REFRESHED`) in `security_events`, whose IP is located
     at scoring time with the current file (at most the 10 newest are tried), and
   * earlier scored payments, whose point is read from the `fraud_events` snapshot.

   Only events within `half the Earth's circumference / max speed` (22.2 h at 900 km/h) are read: an older event
   cannot imply a speed above the threshold whatever the two places.
4. **Rule `IMPOSSIBLE_TRAVEL` (source RULE, +35):** great-circle (haversine) distance over elapsed time. It fires
   only when the distance is **strictly above** `FRAUD_TRAVEL_MIN_DISTANCE_KM` **and** the implied speed is
   **strictly above** `FRAUD_TRAVEL_MAX_SPEED_KMH`. Zero elapsed time with a distance above the floor counts as
   infinitely fast (no division). Both thresholds are validated in `core/config.py` (greater than 0; at most
   100,000 km/h and 20,000 km; NaN and infinity rejected). They are deployment settings, not ADMIN runtime settings:
   ADR-0031 excludes fraud scoring settings from its allow-list, and that is unchanged.
5. **Defaults.** 900 km/h is roughly the cruising speed of a commercial jet airliner; a genuine journey measured
   door to door from the previous event is slower than that, so an accurately placed traveller does not trigger.
   500 km is a policy choice to ignore placement differences within a metropolitan region; it was **not derived
   from a measurement** (none exists) and must be revisited with real or labelled data. Both are configurable.
6. **Weight 35:** alone it reaches STEP_UP (30-59), so a genuine traveller or a misplaced IP costs one TOTP code
   (customers without MFA are escalated to analyst review, as for every STEP_UP; bill payments have no step-up
   flow, so a STEP_UP holds them for review, as for every rule). With NEW_COUNTRY (+20) it stays
   STEP_UP (55); with NEW_DEVICE_OR_NETWORK (+15) on top, or with NEW_PAYEE_LARGE_AMOUNT (+45), it reaches REVIEW.
7. **Missing data never raises and never scores.** No current point, no earlier located event, or no database
   configured: the snapshot records `location_unknown: true` and the rule adds 0. A missing or unreadable file is
   logged (`geoip_city_db_unavailable`) and switches the rule off rather than failing payments.
8. **Evidence (no black box):** the reason cites both places (event type, country and city of the previous event,
   country of this one), both times (UTC), the distance, the elapsed minutes and the implied speed. The snapshot
   keeps `geo_lat`, `geo_lon`, `location_unknown`, `travel_prior_event`, `travel_prior_country`,
   `travel_prior_city`, `travel_prior_lat`, `travel_prior_lon`, `travel_distance_km`, `travel_elapsed_minutes` and
   `travel_speed_kmh` (null when infinite). Baseline model version `baseline-rules-0.4`.

## Accuracy, privacy and operations
* **Accuracy and residual risk.** The point is where DB-IP places an address block, not where the person is.
  VPNs, mobile-carrier gateways, corporate egress and cloud addresses are routinely placed far from the user; a
  customer switching between home broadband and mobile data within minutes can therefore look like a jump of
  hundreds of km (false positive: one extra TOTP). An attacker using a VPN or proxy in the victim's city, or a
  hijacked session used from the attacker's own location after the attacker's own sign-in or refresh, is not
  detected (false negative): the rule compares with the most recent located event, which may be the attacker's.
  No detection or false-positive rate is claimed: there is no labelled evaluation, and the synthetic F4 rows have
  no coordinates.
* **Re-location of sign-ins.** Sign-in IPs are located when a payment is scored, with the current file; a sign-in
  from before a monthly refresh may be placed slightly differently than at the time. Storing a point at sign-in
  would need a schema change and was not judged worth it for a 22-hour window.
* **Privacy.** No new personal data category is collected: the IP was already stored. The derived city-level point
  is kept only in the staff-only fraud snapshot, never next to the raw IP (the snapshot holds the /24 network,
  ADR-0020). Customer responses never contain reasons or scores.
* **Operations.** Monthly refresh (runbook 2.4); about 60 MB download, 121 MB on disk, memory-mapped by the API and
  the worker (shared page cache). A stale file degrades slowly. Attribution "IP Geolocation by DB-IP" wherever the
  reason text is shown (staff alert view).
* **Performance.** Two indexed queries (`security_events (user_id, created_at)`, `fraud_events (customer_id,
  created_at)`) with LIMITs and at most 10 in-memory lookups per payment; covered by a p95 < 100 ms test.
* **Spoofing.** As ADR-0014 / T31: the client IP is trusted only because the API is reachable solely through nginx.
  The rule only ever adds risk.

## Hybrid scorer and ML
`HybridFraudScorer` runs the baseline first and decides on `max(rules, model)`, so IMPOSSIBLE_TRAVEL raises a
hybrid decision exactly as it raises a rules-only one, and its reason and snapshot fields are merged into the
result. The trained models do not see location (`TransferInputs` has no geo field) and are unchanged; offline
training and evaluation (`pbip_ml.fraud`) read synthetic features with no coordinates and are unaffected. The rule
is not added to the `pbip_ml.fraud.rules` registry: that registry works on `FraudFeatures`, which has no location,
and adding one is a contract change for a later ADR once the generator emits coordinates.

## Standing-approval conditions
1. No security, auth, MFA, RBAC, BOLA or audit control is weakened: one rule is added; nothing is removed; the
   customer response is unchanged. 2. No money movement changes: the rule feeds the existing `decide()`; posting,
   holds and lock order are untouched. 3. No migration (the snapshot is JSONB). 4. No new service, paid
   dependency or outbound data: same vendor file and reader as ADR-0014, fetched offline by an operator; no
   request-path network call. 5. No invented figures: vendor facts are quoted with their date and meaning, the
   thresholds are stated as policy defaults, and no accuracy or detection rate is claimed. 6. In scope: Blueprint
   §8 F4 (MUST), FR-45 / UC-09.

## Review after merge (2026-11-03)
#365 reached `main` as `b0f47fb` with the commit title "wip impossible-travel rule". Its only real commit was the
WIP commit the lead made when work was paused, and the squash merge reused that message. The code was complete
and CI was green (14/14), but nobody had reviewed it. The lead reviewed it in full after the merge:

* **Rule off when no city file is set.** With `GEOIP_CITY_DB_PATH` unset or empty,
  `location_resolver_for` returns `NoLocationResolver`, so the payment and bill services fill no `geo_lat`/`geo_lon`
  and `ip_country` is exactly what it was before. `impossible_travel()` returns at its first line without querying
  the database, and the score, reasons and decision are unchanged. The only visible difference is three keys in
  the staff-only snapshot (`geo_lat: null`, `geo_lon: null`, `location_unknown: true`) and the
  `baseline-rules` version number.
* **Existing payment tests.** In `test_transfers.py`, `test_bills.py` and `test_fraud_hybrid.py`, #365 changed only
  the version string. No decision, score or status assertion was changed.
* **Fixed in the follow-up PR:**
  1. `GEOIP_CITY_DB_PATH=`, as written in `.env.example`, was read as `''`, not `None`. On Windows
     `dev.py test-unit` exports the `.env` values, so `test_travel_defaults_and_policy_from_settings` failed. Blank
     GeoIP paths, city and country, are now normalised to `None` in `Settings`. Before this fix the rule was
     already off for `''`, because `location_resolver_for` treats any falsy path as unset.
  2. The reason labelled times as UTC without converting them. They are now converted.

  Both have regression tests that fail without the fix. A new API-level test covers the payment path with the
  rule off: it runs a transfer that would be impossible travel, with no city file and with an empty path, and
  checks it is scored as before.
* **Hybrid bars, re-measured on `main` after #365, #367 and #370** (model v2.2, seed 42 test / 4242 held-out):

  | Bar | Limit | Result |
  |---|---|---|
  | Legitimate new-payee transfers held for review | ≤ 1% | 0.09% / 0.30% |
  | Legitimate new-payee transfers asked to verify (step-up) | ≤ 10% | 7.49% / 7.99% |
  | All legitimate payments held for review | ≤ 0.1% | 0.020% / 0.039% |
  | Fraud caught at review, hybrid vs rules only | — | 0.808 vs 0.202 (held-out hybrid 0.805) |

  IMPOSSIBLE_TRAVEL cannot change these numbers offline. The synthetic transfers carry `ip_country` and `ip_city`
  but no coordinates, so the rule always records `location_unknown` there. Its effect on live false positives
  stays unmeasured, as stated above.

## Consequences
* F4 is scored when the city file is deployed; without it nothing changes except `location_unknown: true` in
  every snapshot.
* Re-evaluate the thresholds and the weight once real or labelled located data exists (data-gen would need
  coordinates for F4 rows).
