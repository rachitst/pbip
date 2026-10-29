# ADR 0015: Minimum demographic data for product recommendations (age band, with consent)
- Status: Accepted
- Date: 2026-10-30
- Deciders: Architecture Team (product owner authorised considering demographics, UC-12)

## Context
UC-12 lists "demographics" as an input to the product recommendation engine (TASK-054). The
product owner authorised using demographics, provided only the minimum necessary data is
collected, with a stated purpose, consent, access control and retention. Today:

* `customers.date_of_birth` exists (nullable) but registration never asks for it, so it is empty for
  every customer; `customers.risk_profile` exists and is unused (TASK-097 will fill it).
* Spending, income patterns and balances are already derived from the ledger (TASK-040/041/044) and
  need no new personal data.

## Decision
1. **Data collected: date of birth only, optional, self-declared.** The recommendation engine
   receives only a derived **age band** (`18-25`, `26-35`, `36-50`, `51-59`, `60+`), never the date.
2. **Purpose (only):** product eligibility and suitability, e.g. senior-citizen deposit rates at
   60+, insurance and loan age limits, excluding products a customer cannot legally hold. Age band is
   an eligibility filter and a reason shown to the customer ("available because you are 60+"); it is
   not used to rank credit offers up or down.
3. **Not collected:** gender, marital status, religion, caste, ethnicity, health, precise location,
   family details, declared income or employer. Income comes from the ledger; anything else needs a
   new decision with a demonstrated eligibility need.
4. **Consent:** a separate, explicit opt-in ("use my date of birth to tailor product
   recommendations"), off by default, recorded with timestamp and policy version in the audit log,
   withdrawable at any time. Without consent, or without a date of birth, recommendations use
   behavioural data only and age-restricted products are simply not offered. Withdrawing consent
   stops use immediately; the customer may also delete the date of birth.
5. **Existing customers:** no values are inferred or backfilled. Age band is `unknown` until the
   customer provides a date of birth and consents.
6. **Access control:** the date of birth is readable only by the customer (own profile) and by the
   recommendation service, which converts it to the band in-process; staff views (RM, analytics
   dashboards) see the age band at most, and aggregate dashboards only counts per band. It is never
   logged (added to the log redaction keys) and never sent to the AI assistant.
7. **Retention:** kept while the customer relationship is active; deleted with the customer record,
   or on request; consent records are kept as audit evidence.

## Alternatives considered
- Infer age from behaviour or synthetic profiles: rejected, fabricated personal data.
- Collect a wider demographic profile for better targeting: rejected, no eligibility need and
  higher discrimination and breach risk (data minimisation).
- Store an age band instead of the date: rejected, a band goes stale; storing the date and deriving
  the band at use time keeps it correct.

## Consequences
TASK-107 implements collection, consent, derivation and the access rules; TASK-054 uses the band
only through it. Recommendations for customers without consent remain fully functional.
