# Sequence diagrams

Five interactions, drawn from the routers and services that implement them. Paths are relative to
`apps/api/src/pbip_api/` unless they start with `apps/`. Every request passes through nginx (`web` container), which
terminates TLS and proxies `/api/` to an API replica; nginx is drawn only where it does something visible.

| Diagram | Source |
|---|---|
| [1. Transfer with hybrid fraud scoring](#1-transfer-with-hybrid-fraud-scoring) | `modules/payments/service.py`, `modules/fraud/hybrid.py`, `modules/fraud/baseline.py`, `modules/ledger/service.py`, ADR-0019 |
| [2. Step-up verification](#2-step-up-verification) | `modules/payments/service.py` (`confirm_step_up`, `_escalate_to_review`), `modules/auth/service.py` (`verify_step_up`), ADR-0036 |
| [3. Assistant question to answer](#3-assistant-question-to-answer) | `modules/assistant/service.py`, `guard.py`, `fallback.py`, `providers.py`, `llm.py`, `tools.py`, ADR-0018, ADR-0037 |
| [4. Statement PDF download](#4-statement-pdf-download) | `modules/statements/router.py`, `modules/statements/service.py`, `apps/web/src/pages/Statements.tsx` |
| [5. Token refresh and revocation](#5-token-refresh-and-revocation-adr-0021) | `modules/auth/router.py`, `modules/auth/service.py`, `modules/auth/dependencies.py`, ADR-0004, ADR-0021 |

## 1. Transfer with hybrid fraud scoring

Exports: [SVG](svg/sequence-transfer-hybrid-scoring.svg), [PNG](png/sequence-transfer-hybrid-scoring.png).
With `FRAUD_SCORER=hybrid` (the local and demo default) the models are loaded once per process at start-up and never
on a request. The rules always run first and are the floor: the model can raise a decision, never lower one. With
`FRAUD_SCORER=rules` (the code default) only the baseline rules run.

<!-- diagram: sequence-transfer-hybrid-scoring -->
```mermaid
sequenceDiagram
    autonumber
    actor C as Customer (browser)
    participant N as nginx
    participant R as payments router
    participant P as PaymentsService
    participant L as ledger.service
    participant H as HybridFraudScorer
    participant B as BaselineRulesScorer
    participant M as pbip_ml HybridScorer (thread pool)
    participant DB as PostgreSQL
    participant RD as Redis

    C->>N: POST /api/v1/transfers, Bearer token, Idempotency-Key, X-Device-ID
    N->>R: proxy to an API replica
    R->>DB: get_current_principal: user ACTIVE and token version current
    R->>P: create_transfer(command, key, request meta)
    P->>DB: pg_advisory_xact_lock(user)
    P->>DB: INSERT idempotency_keys (or read the stored answer and replay it)
    P->>DB: source account owned? payee VERIFIED?
    P->>L: create_transaction(TRANSFER)
    L->>DB: SELECT accounts FOR UPDATE in sorted id order
    L->>DB: INSERT transactions (INITIATED)
    P->>H: score(TransactionContext: amount, payee, device, IP country, city point)
    H->>B: score(context)
    B->>RD: velocity counters for the account window
    alt Redis unavailable
        B->>DB: count the window from transactions instead
    end
    B->>DB: payee age, recent login failures, devices, last located sign-in
    B-->>H: rules result: risk_score, decision, reasons (RULE)
    H->>DB: 30-day debits and credits, last activity, failed sign-ins
    H->>M: score_transfer(online features) within FRAUD_MODEL_BUDGET_MS
    alt model answers in time
        M-->>H: LightGBM probability, Isolation Forest anomaly, SHAP reasons
        H->>H: higher risk of rules and model decides, reasons merged
    else model missing, raises or too slow
        H->>H: keep the rules result, model_version + "+rules-only", reason MODEL_UNAVAILABLE
    end
    H-->>P: FraudScoreResult(risk_score, decision, reasons, model_version, features, latency)
    P->>DB: INSERT fraud_events (feature snapshot)
    alt ALLOW
        P->>DB: daily limit check
        P->>L: post_transaction: balanced entries, POSTED
        P->>DB: INSERT outbox_events TRANSFER_POSTED
    else STEP_UP (interactive, MFA enabled)
        P->>DB: status PENDING_STEP_UP
    else REVIEW (or STEP_UP that nobody can answer)
        P->>L: place_hold: held_amount increased
        P->>DB: PENDING_REVIEW, INSERT fraud_alerts, outbox TRANSFER_UNDER_REVIEW
    else BLOCK
        P->>DB: DECLINED FRAUD_BLOCK, INSERT fraud_alerts, outbox TRANSFER_DECLINED
    end
    P->>DB: store the response on the key, INSERT audit_logs TRANSFER_CREATED
    P->>DB: COMMIT
    P->>RD: record the transfer in the velocity counters (after commit)
    P-->>R: TransferResult
    R-->>C: 201 POSTED, 202 PENDING_STEP_UP or PENDING_REVIEW, 403 DECLINED (no score, no reasons)
```

## 2. Step-up verification

Exports: [SVG](svg/sequence-step-up.svg), [PNG](png/sequence-step-up.png).
The code is not part of the request hash: a retry after a wrong code is the same logical request. A wrong code
commits its own evidence before the idempotency key is claimed, so the key is never left without an answer.

<!-- diagram: sequence-step-up -->
```mermaid
sequenceDiagram
    autonumber
    actor C as Customer (browser)
    participant R as payments router
    participant P as PaymentsService
    participant A as AuthService
    participant L as ledger.service
    participant DB as PostgreSQL

    Note over C: The transfer answered 202 PENDING_STEP_UP and the web app shows the code dialog
    C->>R: POST /api/v1/transfers/{id}/step-up {code}, Idempotency-Key
    R->>P: confirm_step_up(user, transfer id, code, key)
    P->>DB: pg_advisory_xact_lock(user)
    P->>DB: stored answer for this key and action?
    alt already answered
        P-->>C: the stored response, replayed
    end
    P->>DB: transfer owned by this user? (404 otherwise)
    P->>P: still PENDING_STEP_UP and younger than STEP_UP_EXPIRY_MINUTES? (409 otherwise)
    P->>A: verify_step_up(user, code, subject = transfer id)
    alt code wrong
        A->>DB: INSERT security_events STEP_UP_FAILURE, COMMIT
        A-->>P: invalid verification code
        P->>DB: count STEP_UP_FAILURE events for this transfer
        alt fewer than 3
            P-->>C: 422 invalid verification code (retry allowed)
        else third wrong code
            P->>DB: user lock, claim the key, transfer FOR UPDATE
            P->>L: place_hold
            P->>DB: PENDING_REVIEW, INSERT fraud_alerts, outbox TRANSFER_UNDER_REVIEW
            P->>DB: audit TRANSFER_STEP_UP_ESCALATED, store the answer, COMMIT
            P-->>C: 200 PENDING_REVIEW
        end
    else code valid (TOTP, not replayed)
        A->>DB: INSERT security_events STEP_UP_SUCCESS (no commit yet)
        P->>DB: claim the key, transfer FOR UPDATE, re-check the state
        P->>DB: daily limit check
        P->>L: post_transaction: balanced entries, POSTED
        P->>DB: outbox TRANSFER_POSTED, audit TRANSFER_STEP_UP_CONFIRMED, store the answer
        P->>DB: COMMIT
        P-->>C: 200 POSTED
    end
    Note over P,DB: Unanswered: the beat job expire-stale-step-ups (every 60 s) declines the transfer<br/>with STEP_UP_EXPIRED, audits it and notifies the customer. No hold existed, so no money moves.
```

## 3. Assistant question to answer

Exports: [SVG](svg/sequence-assistant.svg), [PNG](png/sequence-assistant.png).
The model only chooses a tool and its arguments; it never sees tool results, history or identity, and it never writes
the answer. The customer id comes from the access token, never from the model. With `LLM_PROVIDER=ollama` (the
default) the chain is Ollama then rules; with `LLM_PROVIDER=groq` it is Groq, then Ollama, then rules. Each provider
gets one attempt per question.

<!-- diagram: sequence-assistant -->
```mermaid
sequenceDiagram
    autonumber
    actor C as Customer (chat drawer)
    participant R as assistant router
    participant S as AssistantService
    participant G as guard and sanitize
    participant F as fallback (keyword rules)
    participant Q as Groq (optional, hosted)
    participant O as Ollama (host)
    participant T as tool registry
    participant DB as PostgreSQL

    C->>R: POST /api/v1/assistant/chat (or /chat/stream for SSE)
    R->>R: customer session and RATE_LIMIT_ASSISTANT_PER_MINUTE
    R->>S: run(customer id from the token, message)
    S->>G: clean_user_message: NFKC, no control or zero-width characters, 500 characters
    S->>G: guard.check: money movement, other people's data, SQL, privilege, instruction override
    alt refused
        S->>DB: store the redacted exchange, audit assistant.refused (reason only)
        S-->>C: fixed refusal text and a link to the right page
    end
    S->>F: clear_route(message)
    alt clear question (one intent, arguments parse)
        F-->>S: tool and arguments, engine "rules" (no model call)
    else unclear question and a model is configured
        opt LLM_PROVIDER=groq
            S->>Q: redacted question and one function per approved tool, tool_choice required
            Q-->>S: tool call, or timeout, 429, 5xx, bad JSON, budget or cooldown
        end
        opt Groq absent or failed
            S->>O: grammar-constrained JSON: pick one tool
            O-->>S: tool choice, or timeout or invalid output
        end
        S->>S: validate the choice with the tool's own Pydantic model
        alt every provider failed
            S->>F: route(message)
            F-->>S: tool and arguments, engine "rules"
        end
    end
    S->>T: invoke(tool, ToolContext(customer id), arguments)
    T->>DB: SET TRANSACTION READ ONLY, then service-layer reads
    T-->>S: structured result
    S->>G: server writes the reply from the result, redact_output masks account numbers, tokens, emails
    S->>DB: assistant_messages (redacted), audit assistant.tool_call (tool, outcome, engine), one assistant.llm_call row per provider tried, COMMIT
    S-->>C: reply, tool name, engine (SSE: tool_call, tool_result, token, done events)
```

## 4. Statement PDF download

Exports: [SVG](svg/sequence-statement.svg), [PNG](png/sequence-statement.png).
Read-only over the ledger. The only writes are evidence: an audit row and a "statement downloaded" notification, so
the customer is told whenever their history leaves the bank.

<!-- diagram: sequence-statement -->
```mermaid
sequenceDiagram
    autonumber
    actor C as Customer
    participant W as web app (Statements page)
    participant R as statements router
    participant AP as accounts policy
    participant S as statements service
    participant L as ledger.service
    participant DB as PostgreSQL

    C->>W: choose account, start and end date, PDF
    W->>R: GET /api/v1/accounts/{id}/statements?start_date&end_date&format=pdf (Bearer token)
    R->>AP: get_owned_account(authenticated customer id, account id)
    AP->>DB: account held by this customer?
    alt not owned or not found
        R-->>W: 404
    end
    R->>S: download(customer, account, period, PDF, clock)
    S->>S: period_bounds: IST bank days, start not after end, not in the future, at most 366 days (422 otherwise)
    S->>L: entries_in_period(account, start, end, limit + 1)
    L->>DB: transaction_entries with balance_after
    alt more entries than the limit
        S-->>W: 422 choose a shorter period
    end
    S->>L: balance_before(start) when the period has no entries
    S->>S: render_pdf: bank header, masked account number, opening and closing balance, paged table
    S->>DB: audit STATEMENT_DOWNLOADED (period, format, entry count), outbox notification, COMMIT
    S-->>R: file bytes, media type, file name with the last 4 digits
    R-->>W: 200 application/pdf, Content-Disposition attachment, Cache-Control no-store
    W-->>C: browser saves the PDF (authenticated blob download)
```

## 5. Token refresh and revocation (ADR-0021)

Exports: [SVG](svg/sequence-token-refresh.svg), [PNG](png/sequence-token-refresh.png).
The access token is a 15-minute JWT kept in memory by the web app. The refresh token is an opaque value in an
HttpOnly, Secure cookie; only its hash is stored. Each use rotates it inside its family (ADR-0004). Every authenticated
request also checks the user's status and token version (ADR-0021), so disabling a user or changing a password ends
every outstanding access token on the next request.

<!-- diagram: sequence-token-refresh -->
```mermaid
sequenceDiagram
    autonumber
    participant W as web app
    participant R as auth router
    participant A as AuthService
    participant D as get_current_principal
    participant DB as PostgreSQL

    Note over W,R: Page load, or an API call answered 401 because the access token expired
    W->>R: POST /api/v1/auth/refresh with the refresh cookie and Origin header
    R->>R: csrf_guard: Origin must be in CORS_ORIGINS (403 otherwise)
    alt no cookie
        R-->>W: 204 signed out, nothing to restore
    end
    R->>A: refresh(raw token)
    A->>DB: SELECT refresh_tokens WHERE token_hash = hash(raw) FOR UPDATE
    alt unknown or revoked
        A-->>W: 401 invalid session
    else already used (a rotated token presented again)
        A->>DB: revoke the whole family, TOKEN_REUSE_DETECTED, COMMIT
        A-->>W: 401 invalid session
    else expired
        A-->>W: 401 session expired
    else user missing or not ACTIVE
        A->>DB: revoke the family, COMMIT
        A-->>W: 401 invalid session
    else valid
        A->>DB: mark used, INSERT the next token in the same family (replaced_by_id), TOKEN_REFRESHED, COMMIT
        A-->>R: new access token (roles, amr, ver = users.token_version) and refresh token
        R-->>W: 200 access token, Set-Cookie with the rotated refresh token
    end

    Note over W,DB: Every authenticated request
    W->>D: Authorization: Bearer access token
    D->>D: verify signature, algorithm allow-list, expiry, token type
    D->>DB: access_token_is_current: user exists, ACTIVE, ver equals users.token_version
    alt current
        D-->>W: request proceeds
    else disabled user or bumped version
        D-->>W: 401, same body as an expired or forged token
    end

    Note over W,DB: Revocation
    W->>R: POST /api/v1/auth/logout (cookie, Origin checked)
    R->>A: logout: revoke this refresh family, LOGOUT, COMMIT
    R-->>W: 204, cookie deleted
    W->>R: POST /api/v1/auth/password-change (current and new password)
    R->>A: change_password: revoke every refresh token of the user and token_version + 1, PASSWORD_CHANGED, COMMIT
    Note over D,DB: Older access tokens now fail the version check on their next request
```

**Extension point.** A new interaction gets its own numbered section with a `<!-- diagram: sequence-... -->` marker,
a row in the table at the top, and an entry in [the index](README.md).
