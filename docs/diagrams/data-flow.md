# Data flow diagrams

Level 0 shows PBIP as one process with the people and systems outside it. Level 1 opens it into its main processes
and the data stores they read and write. Process names follow the API modules (`apps/api/src/pbip_api/modules/`);
data stores are PostgreSQL tables (named as in the [ER diagram](../architecture/er-diagram.md)), Redis and files.
Notation: rounded boxes are processes, cylinders are data stores, plain boxes are external entities.

| Diagram | Source |
|---|---|
| [Level 0: context](#level-0-context) | the HLD system boundary, the role gates, `docker-compose.yml` |
| [Level 1: processes and data stores](#level-1-processes-and-data-stores) | the services and models of each module |

## Level 0: context

Exports: [SVG](svg/dfd-level-0.svg), [PNG](png/dfd-level-0.png).

<!-- diagram: dfd-level-0 -->
```mermaid
flowchart LR
    customer["Retail customer"]
    analyst["Fraud analyst"]
    compliance["Compliance officer"]
    rm["Relationship manager"]
    admin["Administrator"]
    llm["LLM provider<br/>Ollama on the host, Groq optional"]
    files["Offline files<br/>fraud and recommendation models,<br/>DB-IP Lite GeoIP databases"]

    pbip("0. PBIP<br/>banking, fraud, analytics and assistant")

    customer -- "credentials, TOTP codes, transfers, payees,<br/>bill payments, loan and deposit requests,<br/>assistant questions, page-view events" --> pbip
    pbip -- "balances, transactions, statements (PDF, CSV),<br/>outcomes without scores, notifications,<br/>insights, recommendations, assistant replies" --> customer
    analyst -- "alert decisions and notes" --> pbip
    pbip -- "alert queue, scores, reasons, features,<br/>monitoring aggregates" --> analyst
    compliance -- "exception acknowledgements and resolutions" --> pbip
    pbip -- "audit log, chain verification,<br/>exceptions and indicators" --> compliance
    rm -- "outreach log entries" --> pbip
    pbip -- "assigned customers' insights,<br/>eligible products, behaviour dashboard" --> rm
    admin -- "staff users, unlocks, runtime settings,<br/>RM assignments" --> pbip
    pbip -- "lockout status, settings, audit" --> admin
    pbip -- "redacted question, tool schemas" --> llm
    llm -- "tool choice and arguments" --> pbip
    files -- "model artifacts, IP to country and city" --> pbip
```

## Level 1: processes and data stores

Exports: [SVG](svg/dfd-level-1.svg), [PNG](png/dfd-level-1.png).
Every write to balances or entries goes through process 4; every security-relevant change also writes D7 inside the
same transaction. Notifications reach the customer only through the outbox (D6) and the relay job.

<!-- diagram: dfd-level-1 -->
```mermaid
flowchart LR
    customer["Retail customer"]
    staff["Staff: analyst, compliance,<br/>RM, administrator"]
    llm["LLM provider"]
    modelfiles[/"Model and GeoIP files"/]

    p1("1. Authenticate and manage sessions<br/>auth, customers")
    p2("2. Initiate payments<br/>payments, bills, payees, scheduled")
    p3("3. Score fraud and manage alerts<br/>fraud")
    p4("4. Post to the ledger<br/>ledger")
    p5("5. Lend and invest<br/>loans, wealth")
    p6("6. Report and analyse<br/>accounts, statements, analytics,<br/>insights, recommendations")
    p7("7. Answer assistant questions<br/>assistant")
    p8("8. Notify<br/>notifications, outbox relay")
    p9("9. Audit and compliance<br/>compliance")
    p10("10. Collect clickstream<br/>events")

    d1[("D1 Identity<br/>users, roles, refresh_tokens,<br/>mfa_recovery_codes, security_events")]
    d2[("D2 Customers and payees<br/>customers, devices, payees")]
    d3[("D3 Ledger<br/>accounts, transactions, transaction_entries,<br/>holds, idempotency_keys")]
    d4[("D4 Fraud<br/>fraud_events, fraud_alerts")]
    d5[("D5 Products<br/>loans, loan_instalments, fixed_deposits,<br/>wealth and insurance catalogues,<br/>bill_payments, billers, scheduled_transfers")]
    d6[("D6 Outbox and notifications<br/>outbox_events, notifications")]
    d7[("D7 Audit<br/>audit_logs, hash-chained")]
    d8[("D8 Analytics<br/>monthly_category_spending, customer_segments,<br/>clickstream_events, rm_assignments, rm_interactions")]
    d9[("D9 Assistant<br/>assistant_messages, redacted")]
    d10[("D10 Redis<br/>rate limits, velocity counters, task queue")]

    customer -- "credentials, codes" --> p1
    staff -- "credentials, codes" --> p1
    p1 <--> d1
    p1 -- "login rate limits" --> d10
    customer -- "transfer, bill, payee, standing order" --> p2
    p2 <--> d2
    p2 -- "transaction context" --> p3
    modelfiles --> p3
    p3 <-- "history, velocity" --> d3
    p3 <--> d10
    p3 -- "score, reasons, feature snapshot" --> d4
    p3 -- "decision" --> p2
    p2 -- "post, hold, capture, release" --> p4
    p4 <--> d3
    p2 --> d5
    staff -- "alert decision" --> p3
    p3 -- "release or reject" --> p2
    customer -- "loan application, deposit booking or closure" --> p5
    p5 -- "disburse, repay, book, pay out" --> p4
    p5 <--> d5
    customer -- "statement and insight requests" --> p6
    staff -- "insight and dashboard requests" --> p6
    p6 -- "read" --> d3
    p6 -- "read" --> d5
    p6 <--> d8
    p6 -- "statements, insights, recommendations" --> customer
    customer -- "question" --> p7
    p7 -- "redacted question" --> llm
    llm -- "tool choice" --> p7
    p7 -- "read-only tools" --> p6
    p7 --> d9
    p7 -- "reply" --> customer
    customer -- "page-view and click events" --> p10
    p10 --> d8
    p2 -- "event" --> d6
    p5 -- "event" --> d6
    p6 -- "statement downloaded" --> d6
    d6 --> p8
    p8 -- "in-app notifications" --> customer
    p1 --> d7
    p2 --> d7
    p3 --> d7
    p5 --> d7
    p7 --> d7
    d7 --> p9
    d1 --> p9
    d4 --> p9
    p9 -- "audit log, exceptions" --> staff
    p3 -- "alerts, monitoring" --> staff
```

**Extension point.** A new process is a numbered rounded node in level 1 with its flows to the stores it reads and
writes; a new table joins the data store it belongs to, or becomes a new `D` store.
