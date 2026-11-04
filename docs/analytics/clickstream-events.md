# Web clickstream event catalogue (TASK-112, FR-50)

The web app sends these events to `POST /api/v1/events/batch` (TASK-055). The catalogue is code:
`apps/web/src/telemetry/catalogue.ts` lists every event type with the only property keys it may carry and
the values each key accepts. An event with any other key or value is dropped in the browser, before it is
buffered. The behavioural analytics dashboard (TASK-101) reads these events from `clickstream_events`.

## Events

| Event type | Sent when | Properties (all required, nothing else) | Source |
|---|---|---|---|
| `page_view` | A customer route is shown | none | `telemetry/PageViews.tsx` |
| `account_viewed` | An account's transactions are opened | `account_type`: `SAVINGS`, `CHECKING` or `CURRENT` | `pages/Accounts.tsx` |
| `search_performed` | Transaction filters are applied | `search_scope`: `TRANSACTIONS`; `filter_count`: integer 0 to 10 | `pages/AccountTransactions.tsx` |
| `transfer_initiated` | A confirmed transfer, bill payment or standing order is submitted | `transfer_kind`: `TRANSFER`, `BILL_PAYMENT` or `SCHEDULED` | `pages/SendMoney.tsx`, `PayBills.tsx`, `ScheduledTransfers.tsx` |
| `loan_calculator_used` | The loan terms change from the starting terms and settle (debounced) | `loan_type`: `PERSONAL`, `AUTO` or `HOME` | `pages/Loans.tsx` |
| `product_clicked` | A recommendation card is opened | `product_id`: a product UUID from `pbip_ml.recommendations.PRODUCT_CATALOGUE` | `trackProductClicked`; the cards come with TASK-062 |

Every event also has `event_id` (random UUID), `event_type`, `event_timestamp` (UTC), `session_id` (a random
UUID per signed-in visit to the customer area) and, when the page is known, `page_url`.

`page_url` is the **route template** (`/accounts`, or `/x/:id` for a route with a parameter), built from the
route config. The path itself, the query string and the fragment are never sent. A path that only matches the
catch-all "not found" route is not recorded, because its text could be anything a user typed.

Never sent: names, emails, account numbers, ids of accounts, payees, billers or transactions, amounts,
references, search terms or any other free text. The API refuses free text and unknown fields as well.

## Sending

* Only for a signed-in customer, and only inside the customer area. Staff screens are never instrumented.
  Signing out (or leaving for the staff area) discards anything unsent.
* Batches leave every 10 s, and when the tab is hidden or closed (`visibilitychange`, `pagehide`), with
  `fetch(..., { keepalive: true })` and the bearer token. `navigator.sendBeacon` is not used: it cannot send the
  `Authorization` header the API requires.
* Bounds: at most 25 events per batch (also the API's limit, ADR-0055), at most 10 batches per minute, one
  request at a time, and a buffer of 100 events that drops the oldest when full.
* A failed batch (network error, 401, 422, 429, 5xx) is dropped. There is no retry, and a 401 neither refreshes
  the session nor signs the customer out. Tracking never throws into the page.

## Consent

Analytics is off until the customer turns it on (Profile page, `PUT /api/v1/customers/me/analytics-consent`;
ADR-0057). The decision is stored with its time and policy version and audited. The API stores no event for a
customer whose consent is off: the batch is answered 202 with `accepted: 0` and `not_recorded: n`. Turning it off
erases the customer's stored events and behavioural profile. The tracker also refuses when the browser sends the
Global Privacy Control signal.

## Sessions and the behavioural profile

A session is the events of one `session_id` in time order, split wherever more than 30 minutes pass without an
event; its length is last event minus first event. A daily job turns each consenting customer's last 90 days into
a behavioural profile: sessions, mean session length, pages per session, last activity and product interests
(recency-weighted: a recommendation card opened weighs 3, the loan calculator 2, a visit to `/loans`, `/wealth` or
`/wealth/compare` 1, halving every 14 days). ADR-0057 has the full rules.

## Limits

* Product clicks are counted only once TASK-062 renders recommendation cards and calls `trackProductClicked`.
* Session length is derived from the first and last event of a session, so a visit with one event counts as 0 s.
* Events still in the buffer when the customer signs out are discarded.
