# Activity diagrams

Seven flows, each drawn from the service code that runs it. Rounded boxes are actions, diamonds are decisions,
and the boxes with a double border are outcomes the caller sees. HTTP codes are the ones the API returns
(RFC 7807 problem+json for errors). "Rolled back" means the database transaction is discarded and nothing of
the attempt persists, including the idempotency row, so the client may retry with the same key.

| Diagram | Source |
|---|---|
| [1. Login with MFA](#1-login-with-mfa) | `modules/auth/router.py` (`/login`, `/mfa/verify`), `modules/auth/service.py` (`login`, `verify_mfa_login`, `_register_failure`) |
| [2. Transfer with review, step-up and fraud decision](#2-transfer-with-review-step-up-and-fraud-decision) | `modules/payments/service.py` (`_execute_transfer`, `confirm_step_up`, `_escalate_to_review`), `payments/step_up_expiry.py`, `apps/web/src/pages/SendMoney.tsx` |
| [3. Fraud alert management (UC-10)](#3-fraud-alert-management-uc-10) | `modules/fraud/service.py` (`open_alert`), `modules/fraud/router.py`, `PaymentsService.review_alert`, `bills/service.py` (`settle_reviewed_bill`) |
| [4. Loan application and disbursal](#4-loan-application-and-disbursal) | `modules/loans/service.py` (`check_eligibility`, `apply_for_loan`, `collect_due_instalments`) |
| [5. Bill payment](#5-bill-payment) | `modules/bills/service.py` (`pay_bill`) |
| [6. Scheduled transfer run](#6-scheduled-transfer-run) | `modules/payments/scheduled_service.py`, beat entry `execute-scheduled-transfers` |
| [7. Fixed deposit booking and maturity payout](#7-fixed-deposit-booking-and-maturity-payout) | `modules/wealth/service.py` (`book_fixed_deposit`, `close_fixed_deposit`), `modules/wealth/maturity_service.py` |

All paths are relative to `apps/api/src/pbip_api/` unless they start with `apps/`.

## 1. Login with MFA

Exports: [SVG](svg/activity-login-mfa.svg), [PNG](png/activity-login-mfa.png).
Both endpoints share the login rate limiter (`RATE_LIMIT_LOGIN_PER_MINUTE`, default 10 per IP). Failed attempts
commit their evidence (security event, failure counter, lockout) before the error is returned. Customers may enable
MFA; every staff route requires a session whose `amr` contains `otp`.

<!-- diagram: activity-login-mfa -->
```mermaid
flowchart TD
    start(["Customer or staff member submits email and password<br/>POST /api/v1/auth/login"])
    rl{"Login rate limit<br/>exceeded for this IP?"}
    r429[["429 Too Many Requests"]]
    known{"User with this email?"}
    burn["Hash the password anyway (equal timing)<br/>LOGIN_FAILURE reason unknown_user, commit"]
    r401a[["401 invalid credentials"]]
    locked{"locked_until in the future?"}
    blocked["LOGIN_BLOCKED_LOCKED, commit"]
    pwlock{"Password correct?"}
    r423[["423 account temporarily locked"]]
    r401b[["401 invalid credentials"]]
    pwok{"Password correct<br/>and user ACTIVE?"}
    fail["failed_login_count + 1, LOGIN_FAILURE<br/>at the threshold: locked_until with<br/>exponential back-off, ACCOUNT_LOCKED; commit"]
    r401c[["401 invalid credentials"]]
    reset["Reset failure counter<br/>re-hash if Argon2 parameters changed"]
    mfa{"MFA enabled?"}
    challenge["Issue short-lived mfa_challenge token<br/>LOGIN_PASSWORD_OK_MFA_PENDING, commit"]
    r200mfa[["200 mfa_required and mfa_token"]]
    code(["User enters a TOTP or recovery code<br/>POST /api/v1/auth/mfa/verify"])
    rl2{"Login rate limit<br/>exceeded for this IP?"}
    tok{"Challenge token valid<br/>and user not locked?"}
    r401d[["401 invalid verification code"]]
    totp{"TOTP valid, not replayed?"}
    rec{"Unused recovery code?"}
    mfafail["MFA_FAILURE and a counted failure<br/>(may lock the account), commit"]
    r401e[["401 invalid verification code"]]
    recused["Recovery code consumed<br/>RECOVERY_CODE_USED"]
    sess2["Issue session amr pwd+otp:<br/>access JWT with roles and token version,<br/>new refresh token family; LOGIN_SUCCESS, commit"]
    sess1["Issue session amr pwd:<br/>access JWT, new refresh token family;<br/>LOGIN_SUCCESS, commit"]
    done[["200 access token in the body,<br/>refresh token in an HttpOnly Secure cookie"]]

    start --> rl
    rl -- yes --> r429
    rl -- no --> known
    known -- no --> burn --> r401a
    known -- yes --> locked
    locked -- yes --> blocked --> pwlock
    pwlock -- yes --> r423
    pwlock -- no --> r401b
    locked -- no --> pwok
    pwok -- no --> fail --> r401c
    pwok -- yes --> reset --> mfa
    mfa -- no --> sess1 --> done
    mfa -- yes --> challenge --> r200mfa --> code --> rl2
    rl2 -- yes --> r429
    rl2 -- no --> tok
    tok -- no --> r401d
    tok -- yes --> totp
    totp -- yes --> sess2
    totp -- no --> rec
    rec -- yes --> recused --> sess2
    rec -- no --> mfafail --> r401e
    sess2 --> done
```

## 2. Transfer with review, step-up and fraud decision

Exports: [SVG](svg/activity-transfer.svg), [PNG](png/activity-transfer.png).
The review and confirm steps happen in the browser (`SendMoney.tsx`); the server sees one idempotent POST. The lock
order (user advisory lock, idempotency row, accounts `FOR UPDATE` in sorted id order, then the transaction header) is
the deadlock contract in AGENTS.md section 4. The customer response never contains the score or the reasons.

<!-- diagram: activity-transfer -->
```mermaid
flowchart TD
    pick(["Customer picks the source account,<br/>a verified payee or own account, amount and reference"])
    review["Review dialog shows the details<br/>customer confirms"]
    post["POST /api/v1/transfers<br/>Idempotency-Key header, X-Device-ID"]
    max{"Amount above the<br/>per-transfer limit?"}
    r422max[["422 limit exceeded"]]
    lock["Per-user advisory lock"]
    idem{"Idempotency key<br/>already used?"}
    replay[["Stored response replayed<br/>Idempotent-Replayed: true"]]
    reuse[["422 key reused with a different body,<br/>409 if still in progress"]]
    own{"Source account owned<br/>and currency matches?"}
    r404[["404 not found, or 422"]]
    dest{"Destination resolvable?<br/>own account, or VERIFIED payee"}
    r422dest[["422, rolled back"]]
    header["ledger.create_transaction:<br/>accounts locked in sorted order,<br/>header INITIATED"]
    score["FraudScorer.score: rules, then the model<br/>when FRAUD_SCORER=hybrid;<br/>fraud_events row with score, reasons, features"]
    decision{"Decision"}
    canstep{"Interactive request<br/>and MFA enabled?"}
    allowlim{"Daily limit ok and<br/>funds available?"}
    r422lim[["422, rolled back:<br/>nothing persists"]]
    posted["Entries posted, POSTED<br/>notify TRANSFER_POSTED"]
    r201[["201 POSTED"]]
    stepup["PENDING_STEP_UP<br/>no hold, no entries"]
    r202s[["202 PENDING_STEP_UP"]]
    revlim{"Daily limit ok and<br/>funds available?"}
    hold["Hold placed, PENDING_REVIEW,<br/>alert opened, notify TRANSFER_UNDER_REVIEW"]
    r202r[["202 PENDING_REVIEW"]]
    block["DECLINED, FRAUD_BLOCK,<br/>alert opened, notify TRANSFER_DECLINED"]
    r403[["403 DECLINED"]]
    finish["Store the response on the key,<br/>audit TRANSFER_CREATED, commit;<br/>metrics and velocity counters after commit"]

    totpin(["Customer enters a TOTP code<br/>POST /api/v1/transfers/id/step-up with Idempotency-Key"])
    waiting{"Still PENDING_STEP_UP and<br/>younger than STEP_UP_EXPIRY_MINUTES?"}
    r409[["409 not waiting for verification"]]
    valid{"Code valid?"}
    fails{"Third wrong code<br/>for this transfer?"}
    r422code[["422 invalid code<br/>STEP_UP_FAILURE committed; retry"]]
    escal["Hold placed, PENDING_REVIEW,<br/>alert opened, TRANSFER_STEP_UP_ESCALATED"]
    confirm["Daily limit re-checked, entries posted,<br/>POSTED, TRANSFER_STEP_UP_CONFIRMED"]
    r200[["200 POSTED, or 200 PENDING_REVIEW"]]
    expiry["Beat every 60 s: expire-stale-step-ups<br/>DECLINED with STEP_UP_EXPIRED, notified, audited"]
    analyst["Analyst decision<br/>see diagram 3"]

    pick --> review --> post --> max
    max -- yes --> r422max
    max -- no --> lock --> idem
    idem -- "same body, answered" --> replay
    idem -- "different body or in progress" --> reuse
    idem -- new --> own
    own -- no --> r404
    own -- yes --> dest
    dest -- no --> r422dest
    dest -- yes --> header --> score --> decision
    decision -- ALLOW --> allowlim
    allowlim -- no --> r422lim
    allowlim -- yes --> posted --> finish --> r201
    decision -- STEP_UP --> canstep
    canstep -- yes --> stepup --> finish
    finish --> r202s
    canstep -- "no: becomes REVIEW" --> revlim
    decision -- REVIEW --> revlim
    revlim -- no --> r422lim
    revlim -- yes --> hold --> finish
    finish --> r202r
    decision -- BLOCK --> block --> finish
    finish --> r403

    r202s --> totpin --> waiting
    r202s -. "no answer in time" .-> expiry
    waiting -- no --> r409
    waiting -- yes --> valid
    valid -- no --> fails
    fails -- no --> r422code
    fails -- yes --> escal --> r200
    valid -- yes --> confirm --> r200
    r202r --> analyst
```

## 3. Fraud alert management (UC-10)

Exports: [SVG](svg/activity-fraud-alert.svg), [PNG](png/activity-fraud-alert.png).
An alert is opened in the same database transaction as the decision that caused it. Alerts are not assigned in
advance: the analyst who resolves one is stored on it. The analyst routes require `FRAUD_ANALYST` or `ADMIN` with an
MFA session (`analyst_gate`).

<!-- diagram: activity-fraud-alert -->
```mermaid
flowchart TD
    subgraph open["Alert opened, same transaction as its cause"]
        o1["Transfer or bill payment scored REVIEW or BLOCK"]
        o2["Third wrong step-up code"]
        o3["Hourly structuring monitor finds<br/>structured deposits, credit already POSTED"]
        alert["fraud_alerts row OPEN<br/>severity HIGH when risk_score is 85 or more, else MEDIUM"]
    end
    notify["Customer told through the outbox:<br/>under review, or declined;<br/>relay every 10 s writes the in-app notification"]
    signin(["Analyst signs in with password and TOTP"])
    queue["Alert queue GET /api/v1/fraud/alerts<br/>newest first, filter by status;<br/>monitoring page GET /fraud/monitoring"]
    detail["Alert detail GET /fraud/alerts/id:<br/>score, decision, reasons with source<br/>RULE / MODEL / ANOMALY, features, model version"]
    decide(["Analyst chooses RELEASE or REJECT with a note<br/>POST /fraud/alerts/id/decision, Idempotency-Key"])
    lock["Analyst advisory lock, idempotency key;<br/>a replay returns the stored 204"]
    found{"Alert exists?"}
    r404[["404"]]
    isopen{"Alert still OPEN?"}
    r409[["409 already resolved"]]
    txstate{"Transaction status"}
    rel{"RELEASE?"}
    capture["Hold captured, entries posted<br/>transaction POSTED"]
    release["Hold released<br/>transaction DECLINED, ANALYST_REJECTED"]
    label["Label only: no money moves<br/>engine BLOCK stays DECLINED,<br/>monitor alert credit stays POSTED"]
    status["Alert FALSE_POSITIVE on RELEASE,<br/>CONFIRMED_FRAUD on REJECT;<br/>resolver, note, resolved_at stored"]
    bill{"Bill payment?"}
    settle["settle_reviewed_bill mirrors the outcome<br/>on bill_payments and sends the bill notification"]
    tell["Customer notified TRANSFER_POSTED or<br/>TRANSFER_DECLINED, except for a monitor alert"]
    audit["Audit FRAUD_ALERT_RESOLVED, commit"]
    r204[["204 No Content"]]

    o1 --> alert
    o2 --> alert
    o3 --> alert
    alert --> notify
    alert --> signin --> queue --> detail --> decide --> lock --> found
    found -- no --> r404
    found -- yes --> isopen
    isopen -- no --> r409
    isopen -- yes --> txstate
    txstate -- PENDING_REVIEW --> rel
    rel -- yes --> capture --> status
    rel -- no --> release --> status
    txstate -- "DECLINED or POSTED" --> label --> status
    status --> bill
    bill -- yes --> settle --> audit
    bill -- no --> tell --> audit
    audit --> r204
```

## 4. Loan application and disbursal

Exports: [SVG](svg/activity-loan.svg), [PNG](png/activity-loan.png).
There is no human approval step: the eligibility check is re-run inside the application and decides (ADR-0024).
Disbursal, the loan, every instalment and the repayment instruction are written in one commit.

<!-- diagram: activity-loan -->
```mermaid
flowchart TD
    browse(["Customer opens /loans<br/>GET /api/v1/loans/products"])
    emi["POST /loans/calculate-emi<br/>EMI and amortisation schedule"]
    elig["POST /loans/check-eligibility:<br/>monthly income from recurring credits,<br/>existing PBIP EMIs, debt-to-income with the new EMI"]
    show[["ELIGIBLE or NOT_ELIGIBLE with reasons<br/>and the largest amount per tenure"]]
    apply(["Customer confirms<br/>POST /loans/apply with Idempotency-Key"])
    terms{"Product, tenure and<br/>amount offered?"}
    r4xx[["404 product not found, or 422"]]
    lock["Per-user advisory lock, idempotency key<br/>a replay returns the stored 201"]
    acct{"Account owned, ACTIVE, INR?"}
    r422a[["422, or 404 when not owned"]]
    rerun{"Eligibility re-run:<br/>income found, DTI at most 50%,<br/>no lockout in 90 days?"}
    r422e[["422 not eligible, with reasons;<br/>rolled back"]]
    disburse["ledger.disburse_loan:<br/>LOAN_DISBURSAL, debit SYS-CLEARING,<br/>credit the customer account, POSTED"]
    book["loans row ACTIVE;<br/>one loan_instalments row per month, DUE"]
    instr["Monthly repayment instruction:<br/>scheduled_transfers row with loan_id, ACTIVE"]
    note["Notify LOAN_DISBURSED; audit LOAN_DISBURSED;<br/>store the response on the key; commit"]
    r201[["201 loan with its schedule"]]
    tick["Beat every 60 s runs due instructions<br/>see diagram 6"]
    collect{"Instalments DUE on or before today:<br/>funds available and account ACTIVE?"}
    wait["Insufficient funds: rolled back, stays DUE,<br/>failure counted, retried next tick as arrears;<br/>inactive account: instruction PAUSED"]
    repay["LOAN_REPAYMENT posted to SYS-CLEARING;<br/>instalments PAID"]
    last{"Any DUE instalment left?"}
    closed["Loan CLOSED,<br/>instruction COMPLETED"]
    next["next_run_date moved to the next due date"]

    browse --> emi --> elig --> show --> apply --> terms
    terms -- no --> r4xx
    terms -- yes --> lock --> acct
    acct -- no --> r422a
    acct -- yes --> rerun
    rerun -- no --> r422e
    rerun -- yes --> disburse --> book --> instr --> note --> r201
    r201 -.-> tick --> collect
    collect -- no --> wait
    collect -- yes --> repay --> last
    last -- no --> closed
    last -- yes --> next
```

## 5. Bill payment

Exports: [SVG](svg/activity-bill-payment.svg), [PNG](png/activity-bill-payment.png).
Bill payments use the same fraud contract, limits and lock order as transfers, with two differences: the money goes
to the clearing account, and there is no step-up flow, so `STEP_UP` is held for review like `REVIEW`.

<!-- diagram: activity-bill-payment -->
```mermaid
flowchart TD
    billers(["Customer opens /bills<br/>GET /api/v1/bills/billers"])
    pay(["Customer enters biller, consumer reference,<br/>amount and account; POST /bills/pay with Idempotency-Key"])
    max{"Amount above the<br/>per-payment limit?"}
    r422m[["422 limit exceeded"]]
    lock["Per-user advisory lock, idempotency key<br/>a replay returns the stored response"]
    acct{"Account owned and<br/>currency matches?"}
    r4a[["404 or 422"]]
    biller{"Biller exists and ACTIVE?"}
    r4b[["404 not found, or 422 inactive"]]
    header["ledger.create_transaction BILL_PAYMENT<br/>debit the customer, credit SYS-CLEARING,<br/>reference BILL/code/consumer reference"]
    score["FraudScorer.score, fraud_events row"]
    decision{"Decision"}
    lim1{"Daily limit ok and<br/>funds available?"}
    lim2{"Daily limit ok and<br/>funds available?"}
    r422[["422, rolled back"]]
    posted["Entries posted<br/>bill payment POSTED"]
    held["Hold placed, transaction PENDING_REVIEW,<br/>alert opened; bill payment PENDING_REVIEW"]
    declined["Transaction DECLINED FRAUD_BLOCK,<br/>alert opened; bill payment DECLINED"]
    row["bill_payments row; outbox notification<br/>posted, under review or declined"]
    done["Store the response on the key,<br/>audit BILL_PAYMENT_CREATED, commit"]
    r[["201 POSTED, 202 PENDING_REVIEW<br/>or 403 DECLINED"]]
    analyst["Analyst decision settles the bill payment<br/>see diagram 3"]

    billers --> pay --> max
    max -- yes --> r422m
    max -- no --> lock --> acct
    acct -- no --> r4a
    acct -- yes --> biller
    biller -- no --> r4b
    biller -- yes --> header --> score --> decision
    decision -- ALLOW --> lim1
    lim1 -- no --> r422
    lim1 -- yes --> posted --> row
    decision -- "STEP_UP or REVIEW" --> lim2
    lim2 -- no --> r422
    lim2 -- yes --> held --> row
    decision -- BLOCK --> declined --> row
    row --> done --> r
    held -.-> analyst
```

## 6. Scheduled transfer run

Exports: [SVG](svg/activity-scheduled-run.svg), [PNG](png/activity-scheduled-run.png).
Celery beat publishes `pbip.transfers.execute_scheduled` every 60 seconds and a worker runs it. Each instruction
runs in its own database session and transaction, so one failure never affects another. The transfer itself is the
same `_execute_transfer` as an interactive one, with `channel=SCHEDULED`, `interactive=False` (a `STEP_UP` becomes
`REVIEW`) and the idempotency key `sched-<instruction id>-<date>`.

<!-- diagram: activity-scheduled-run -->
```mermaid
flowchart TD
    beat(["Beat: execute-scheduled-transfers, every 60 s"])
    select["Select ACTIVE instructions with next_run_date on or before today,<br/>oldest first, up to SCHEDULED_TRANSFERS_BATCH_SIZE"]
    each["For each instruction, a new session"]
    trylock{"Instruction advisory<br/>lock acquired?"}
    skipl[["SKIPPED_LOCKED: another worker has it"]]
    ulock["Per-user advisory lock, row FOR UPDATE"]
    due{"Still ACTIVE and due?"}
    skipi[["SKIPPED_INACTIVE"]]
    loan{"Loan repayment<br/>instruction?"}
    collect["Collect due and overdue instalments<br/>see diagram 4"]
    past{"next_run_date before today?"}
    missed["Missed dates audited SCHEDULED_TRANSFER_MISSED,<br/>next_run_date rolled forward or COMPLETED,<br/>customer notified; commit: RECOVERED"]
    cust{"Customer exists?"}
    pausec["PAUSED, CUSTOMER_ACTION_REQUIRED,<br/>audited; commit"]
    exec["_execute_transfer, non-interactive,<br/>key sched-id-date, fraud scored"]
    outcome{"Outcome"}
    transient["Insufficient funds or daily limit: rolled back;<br/>new transaction: failure_count + 1, stays ACTIVE,<br/>notified once per day, retried next tick"]
    permanent["Account not active, payee gone or unverified,<br/>other domain error: rolled back; new transaction:<br/>PAUSED CUSTOMER_ACTION_REQUIRED, audited, notified"]
    replay["Key already answered: REPLAY, commit"]
    advance["POSTED or PENDING_REVIEW: next_run_date advanced,<br/>or COMPLETED after the last occurrence;<br/>failure_count reset; commit"]
    blocked["DECLINED by fraud BLOCK: PAUSED FRAUD_BLOCK,<br/>or COMPLETED if nothing is left;<br/>audited, notified; commit"]
    clear["Analyst may clear the FRAUD_BLOCK<br/>POST /fraud/scheduled-transfers/id/clear;<br/>the customer must still resume it"]

    beat --> select --> each --> trylock
    trylock -- no --> skipl
    trylock -- yes --> ulock --> due
    due -- no --> skipi
    due -- yes --> loan
    loan -- yes --> collect
    loan -- no --> past
    past -- yes --> missed
    past -- "no: due today" --> cust
    cust -- no --> pausec
    cust -- yes --> exec --> outcome
    outcome -- "InsufficientFunds or LimitExceeded" --> transient
    outcome -- "NotActive, NotFound, Unprocessable, Conflict" --> permanent
    outcome -- replayed --> replay
    outcome -- "POSTED or PENDING_REVIEW" --> advance
    outcome -- DECLINED --> blocked -.-> clear
```

## 7. Fixed deposit booking and maturity payout

Exports: [SVG](svg/activity-fixed-deposit.svg), [PNG](png/activity-fixed-deposit.png).
Booking and early closure are customer requests; the maturity payout is the hourly beat job
`pay-out-matured-deposits` (minute 5). The payout uses one idempotency key per deposit, `fd-maturity/<deposit id>`,
which no client request can use, so a deposit is paid at most once (ADR-0029).

<!-- diagram: activity-fixed-deposit -->
```mermaid
flowchart TD
    quote(["Customer picks a deposit product, amount and tenure<br/>POST /api/v1/wealth/fixed-deposits/quote: exact maturity preview"])
    book(["POST /wealth/fixed-deposits/book with Idempotency-Key"])
    lock["Per-user advisory lock, idempotency key<br/>a replay returns the stored response"]
    acct{"Funding account owned,<br/>ACTIVE and INR?"}
    r422a[["422, or 404 when not owned"]]
    terms{"Product bookable online,<br/>tenure and amount offered?"}
    r4t[["404 product not found, or 422"]]
    post{"Funds available?"}
    r422f[["422 insufficient funds, rolled back"]]
    booked["ledger.book_fixed_deposit: FD_BOOKING,<br/>debit the customer, credit SYS-FIXED-DEPOSITS;<br/>fixed_deposits row ACTIVE with the projected maturity"]
    bnote["Notify FIXED_DEPOSIT_BOOKED, audit, commit"]
    r201[["201 deposit"]]

    early(["Customer closes it<br/>POST /wealth/fixed-deposits/id/close with Idempotency-Key"])
    isclosed{"Already CLOSED?"}
    r409[["409 already closed"]]
    when{"On or after the maturity date?"}
    lockin{"Premature closure allowed<br/>and lock-in period over?"}
    r422l[["422 cannot be closed before the lock-in date"]]
    matured["Interest = projected maturity interest<br/>closure MATURED"]
    premature["Interest at the penalty-reduced rate<br/>for the time held; closure PREMATURE"]
    payout["ledger.pay_out_fixed_deposit:<br/>FD_CLOSURE principal and FD_INTEREST;<br/>deposit CLOSED; notify, audit, commit"]
    r200[["200 closed deposit"]]

    hourly(["Beat: pay-out-matured-deposits, hourly at minute 5"])
    dueids["Select ACTIVE deposits whose maturity date<br/>is on or before the bank date"]
    key{"Key fd-maturity/id<br/>already answered?"}
    rep[["REPLAY"]]
    still{"Deposit still ACTIVE<br/>and matured, under row lock?"}
    skip[["SKIPPED"]]
    active{"Payout account ACTIVE?"}
    paid["FD_CLOSURE and FD_INTEREST posted,<br/>deposit CLOSED MATURED; notify FIXED_DEPOSIT_MATURED,<br/>audit FIXED_DEPOSIT_MATURED_PAID_OUT, commit: PAID"]
    first{"Payout already<br/>marked held?"}
    held["payout_held_at set; notify FIXED_DEPOSIT_PAYOUT_HELD,<br/>audit, commit: HELD; retried every hour"]
    stillheld[["STILL_HELD, nothing written"]]

    quote --> book --> lock --> acct
    acct -- no --> r422a
    acct -- yes --> terms
    terms -- no --> r4t
    terms -- yes --> post
    post -- no --> r422f
    post -- yes --> booked --> bnote --> r201

    r201 -.-> early --> isclosed
    isclosed -- yes --> r409
    isclosed -- no --> when
    when -- yes --> matured --> payout
    when -- no --> lockin
    lockin -- no --> r422l
    lockin -- yes --> premature --> payout
    payout --> r200

    r201 -.-> hourly --> dueids --> key
    key -- yes --> rep
    key -- no --> still
    still -- no --> skip
    still -- yes --> active
    active -- yes --> paid
    active -- "no: rolled back" --> first
    first -- no --> held
    first -- yes --> stillheld
```

**Extension point.** A new flow gets its own numbered section with a `<!-- diagram: activity-... -->` marker, a row in
the table at the top, and an entry in [the index](README.md).
