# Mutation testing report: ledger and payments (TASK-086, #109)

**Result: 1,041 of 1,076 mutants killed = 96.7%** (target ≥ 85%). **No critical mutant survives.**
The 35 survivors are listed and classified below.

| Module | Mutants | Killed | Survived | Score |
|---|---:|---:|---:|---:|
| `modules/ledger/service.py` | 330 | 322 | 8 | 97.6% |
| `modules/payments/service.py` | 746 | 719 | 27 | 96.4% |
| **Total** | **1,076** | **1,041** | **35** | **96.7%** |

Before this task the same suite scored **68.2%** on the 500 mutants it reached (341 killed and
159 survived; the run was stopped at 500/1076 to free the machine). The first full run with the
first batch of new tests scored **81.6%** (878/1076). Two more rounds of targeted tests took it to
96.7%.

Measured on **this laptop: Intel Celeron N4500, 2 cores, 7 GB RAM, PostgreSQL 16 in Docker**. This
is not staging and not CI. Base commit `cdda170` (branched from main at `98b8603`).

## How to run it

```bash
python scripts/dev.py infra-up          # PostgreSQL for the test database
uv run python -m scripts.mutation_gate  # full run, then fails below 85% or if any mutant was not run
uv run python -m scripts.mutation_gate --score-only   # re-score the last full run
```

The gate sets `DATABASE_URL` to the **test** database, then runs
`mutmut run --max-children 1` from `apps/api` and scores `mutmut results --all true`. The
configuration is `[tool.mutmut]` in `apps/api/pyproject.toml`:

* `only_mutate`: the two service modules above.
* `pytest_add_cli_args_test_selection`: the ledger, guards, transfers, transfer-contract, bills
  and pagination integration tests, plus the hash, ledger-initialisation and money unit tests.
* `process_isolation = "forkserver"` and `also_copy = ["alembic.ini", "alembic/"]`.

A full run took **3 h 03 min** on this laptop (02:07 to 05:10, about 0.10 mutants/s). The
gate is a documented command, not a per-PR CI job, because of that duration.

## Pitfalls (each one produced an invalid result first)

| Run | What happened | Fix |
|---|---|---|
| 1 | Every mutant "killed": the copied tree had no `alembic.ini`, so every test errored | `also_copy = ["alembic.ini", "alembic/"]` |
| 2 | 1076/1076 "killed": with fork isolation every mutant died | `process_isolation = "forkserver"` |
| parallel | False kills: workers share one test database and the autouse `clean_db` truncation wipes another worker's rows mid-test | `--max-children 1` (one worker) |
| manual | `MUTANT_UNDER_TEST=<name> pytest` from `mutants/` imported the **installed** `src/` package, so the mutant was never applied and everything "survived" | prefix `PYTHONPATH=$PWD/src`; check with a mutant known to be killed first |
| re-runs | `mutmut run <names>` re-runs only the named mutants and reports all others as `not checked` | score composed from the runs below; the gate refuses a run with unchecked mutants |

mutmut 3.8.1 maps modules only from `./src`, so the config lives in `apps/api` rather than at the
repository root. Manual mutant checks used a separate database (`pbip_test_mut_side`), never the
one a run was using.

## How the score was reached

| Step | Mutants run | Killed | Survived | Wall time |
|---|---:|---:|---:|---|
| Full run with the first batch of new tests | 1,076 | 878 | 198 | 02:07 to 05:10 |
| The 198 survivors, after the contract and hash tests | 198 | 131 | 67 | 05:16 to 06:43 |
| The 67 survivors, after the ledger-guard and daily-limit tests | 67 | 28 | 39 | 06:49 to 07:40 |
| 4 entry-listing mutants, after the listing test | 4 | 2 | 2 | 07:41 to 07:43 |
| 2 `lock_accounts` mutants, after the stale-balance test | 2 | 2 | 0 | about 2 min |

Each step only **added** tests or assertions; none was removed or weakened. A mutant killed in an
earlier step is therefore still killed. A single clean full run would confirm the number; it was
not repeated because it takes 3 hours on this machine.

## Tests added (what each catches)

`tests/integration/test_transfer_contract.py` (new):

* **Response body:** the exact 201 transfer body, every field and value. Renamed keys, a missing
  `from_account_id`/`payee_id`/`posted_at` and a wrong `created_at` all went unnoticed before.
  A transfer between own accounts reports no payee.
* **Audit row:** `TRANSFER_CREATED` names the actor, resource, IP and request id, and its
  `details` hold the status, decision, risk score and amount.
* **Fraud engine input:** the context includes the device id, IP country, `/24` network and
  payee.
* **Metrics:** `transfers_total{status}` and `fraud_decisions_total{decision}` increase, and
  `fraud_inference_seconds` records milliseconds / 1000.
* **Notifications:** each outcome (posted, under review, declined) notifies the sending user.
* **Idempotency:**
  * The row stores this user's request hash and expires in the future.
  * A replay finds its own row among other keys and another user's identical key.
  * A key without a complete stored response is "still in progress".
  * Reusing a key for a different request is rejected with the exact message.
* **Error messages:** every destination error carries its exact message: per-transfer limit,
  currency mismatch, same account (own account or payee), payee pointing at a SYSTEM account,
  unverified payee.
* **Daily limit:** counts only this user's live transfers. Another customer's spending and the
  user's own declined transfer do not count.

`tests/unit/test_transfer_request_hash.py` (new): the idempotency hash is stored, so its
canonical JSON is now pinned. A change in field names, separators or key order would make
retries spanning a deploy fail as "a different request".

`tests/integration/test_transfers.py` (extended):

* **Analyst decisions:** they record who decided and why, and notify the customer. Decision
  errors are specific.
* **Daily-limit boundaries:** the window starts exactly at midnight UTC, and the limit is
  inclusive.
* **Step-up:**
  * It is scoped to the owner and to that transfer.
  * Confirmations and escalations are audited, notified and counted.
  * Wrong codes count per transfer.
  * A second escalation after one already won holds the money only once.
* **Daily-limit recheck:**
  * The limit is rechecked when a held transfer finally moves.
  * A held transfer within the limit is **not counted twice** when it moves (confirm or
    escalate).
  * A transfer held for review counts the day's earlier spending.
* **Holds and messages:** holds accumulate up to exactly the balance, and the customer sees the
  exact plain message for each outcome.

`tests/integration/test_ledger_guards.py` (extended):

* **Exact error messages:** reversal, hold-capture, posting, currency and inactive-account
  guards.
* **One-sided headers:** headers missing either leg are refused by `transfer_legs`,
  `place_hold`, reversal and capture.
* **Smallest amount:** a transfer of **1 minor unit** posts.
* **Deposit defaults:** a deposit's default reference and channel.
* **Entry listing:** the query is bounded and returns newest first by default.
* **Stale balance:** locking reads the committed balance, not the one already cached in the
  session.

## Critical mutants found and killed

These were real gaps: the code was right, but no test would have caught a regression.

| Mutant | What the mutant broke | Killed by |
|---|---|---|
| `lock_accounts` 6, 11 (`populate_existing` dropped) | The ownership check loads the account before the `FOR UPDATE`. Without a refresh, a concurrent spend is invisible: overdraft plus a lost update of the other debit | `test_locking_reads_the_committed_balance_not_the_one_already_in_the_session` |
| `_enforce_daily_limit` 25 (`id != transaction.id` dropped) | A held transfer counts itself twice, so a transfer within the limit is refused on confirm or escalate | `test_a_held_transfer_within_the_limit_is_not_counted_twice_when_it_moves` |
| `create_transfer` 130 (REVIEW path checks the limit for no user) | Transfers held for review ignore the day's spending | `test_a_transfer_held_for_review_counts_the_days_earlier_spending` |
| `_enforce_daily_limit` 21 and 23 | Another user's transfers, or declined ones, use up the limit | `test_the_daily_limit_counts_only_this_users_live_transfers` |
| `post_transaction` 7 (`<= 0` became `<= 1`) | 1-paisa transfers rejected | `test_the_ledger_posts_a_single_minor_unit` |
| `transfer_legs` 1 (`or` became `and`) | A header missing one leg reaches posting | `test_legs_and_holds_refuse_a_header_without_an_account` |
| `_begin_idempotent` 26 and 27 | A replay looks up another user's key, or another of the same user's keys | `test_a_replay_finds_its_own_row_among_other_keys_and_other_users` |
| `create_transfer` 67 to 83 | The fraud engine loses the device, network or country signal | `test_creating_a_transfer_audits_scores_counts_and_notifies_exactly` |

## The 35 survivors, classified

None of them can change money, authorisation or an audit record.

| Survivors | Mutation | Classification and why |
|---|---|---|
| `_active_hold` 3, 7; `reverse_transaction` 3, 7; `review_alert` 3, 7, 18, 22; `_owned_transaction` 11, 12 | `populate_existing` set to None or False | **Equivalent at the current call sites.** In each case the locking query is the first load of that row in the session, or the row is already locked by the same transaction (escalation after a failed confirm), so there is nothing stale to refresh. `lock_accounts`, whose row *is* loaded first, is tested above. |
| `confirm_step_up` 5, 8, 9; `_escalate_to_review` 5, 8, 9 | Row lock not taken | **Equivalent:** the per-user advisory lock (`_lock_user`, always first) already serialises one user's requests. Analysts act only on `PENDING_REVIEW`, never on `PENDING_STEP_UP`. |
| `_owned_transaction` 1 | Plain reads also lock | **Performance only:** same results, more row locks. |
| `create_transfer` 5; `_lock_user` 11 | Advisory lock on `'None'` instead of the user | **Performance only:** one global lock is stricter serialisation with the same results. It would hurt throughput, which the k6 suite (TASK-078) measures. |
| `_lock_user` 7 | SQL keyword case | **Equivalent:** SQL keywords are case-insensitive. |
| `_begin_idempotent` 3 | `ON CONFLICT` without a named constraint | **Equivalent:** the only other unique index is the primary key, a fresh `uuid4`. |
| `_begin_idempotent` 9 | `id` omitted from the insert | **Equivalent:** the column's Python default generates the same UUID. |
| `create_transaction` 17, 28 | `channel` None or omitted | **Equivalent:** the column default `"WEB"` applies; the deposit test asserts `"WEB"`. |
| `_store_idempotent_response` 7, 8, 9 | Message of a `RuntimeError` | **Unreachable:** the row was inserted earlier in the same transaction under the user's lock. |
| `_enforce_daily_limit` 22 | Type filter dropped | **Equivalent today:** only TRANSFER and BILL_PAYMENT carry a customer as `initiated_by_user_id`. Deposits and fees have none; reversals name the staff member. Revisit if a new customer-initiated type is added. |
| `_build_result` 38 | `debit_account_id or True` | **Equivalent:** a transfer always has a debit account. |
| `_step_up_failures` 6, 7, 18 | Filter dropped; `count or 1` | **Equivalent:** only STEP_UP_FAILURE events carry this transaction as `subject`, and `COUNT(*)` is never NULL. |
| `_escalate_to_review` 23 | `order_by(None)` | **Equivalent:** a transaction has exactly one fraud event. |
| `list_account_entries` 10, 12 | Join condition dropped | **Equivalent:** SQLAlchemy infers the same join from the single foreign key. |

## Not verified

* **No clean full run:** the 96.7% comes from one full run plus targeted re-runs of the
  survivors (see the step table). A clean full run (3 h here) would confirm it.
* **Not on a CI runner:** the gate is a documented command, not a per-PR CI job.
* **Two modules only:** other modules (auth, bills, loans) were not mutated, per the task scope.
