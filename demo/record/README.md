# Demo recordings

Two Playwright recordings drive the real web UI on the running stack (`https://localhost:8443`). Both
are recordings, not test suites: one long run, no retries, and they stop at the first step whose API
answer the story does not allow. They reuse the Playwright of `apps/web` (no npm dependency of their own).

| Recording | Script | Config | Length |
|---|---|---|---|
| Fallback demo video (TASK-093) | `record-demo.ts` | `playwright.config.ts` | about 5 minutes; see `demo/demo-script.md` section 7.1 |
| A-to-Z product walkthrough (item 14) | `record-walkthrough.ts` (+ `walkthrough-kit.ts`) | `walkthrough.config.ts` | 10 to 15 minutes after editing, plus a 3-minute highlights cut |

Outputs go to `DEMO_VIDEO_DIR` (default `demo/record/out/`, which git ignores). Videos are attached to
the release; they are never committed.

## The A-to-Z walkthrough

### What it shows, in order

1. **Registration and MFA enrolment:** a new customer registers and sets up an authenticator. The key,
   its `otpauth:` link and the recovery codes are blurred in the page itself (inline `filter: blur`, set
   through the CSSOM before the first paint), so they never reach the video.
2. **Signing in with MFA:** password, a wrong code refused (`mfa/verify` 400/401), then the right code.
3. **Alice:** sign-in with her code; accounts and total balance; transaction search and filters; a PDF
   statement; payees (add, edit, verify); a transfer held for review; a transfer with a step-up code and its
   receipt; a bill payment; a scheduled transfer; notifications; spending and financial health; the balance
   forecast; loans (loans for you, EMI, eligibility, application, repayment schedule); a fixed deposit;
   investing (a fund, a monthly SIP, a stock, a sale, the SIP list); the portfolio; an insurance quote and
   application; the product comparison with the risk-band row; recommendations with reasons; seven assistant
   questions (balance, last month's spending, unusual expenses, loan affordability, investment
   suggestions, an FAQ, and another customer's data, refused); profile, risk profile, consent, the dark
   theme, the phone layout (390x844) and a staff page refused (the page says "not found", the API 403).
4. **Fraud analyst** (password + code): security monitoring; the fraud demo lab F1, F2, F3, F5 and F6 (F4
   explained, not runnable); the alert queue; Alice's alert with rule points and SHAP reasons, rejected; F1
   released; the F6 forward (`MULE_PASS_THROUGH`) rejected; the other lab alerts labelled; the fraud
   dashboard (decisions, rates, risk bands, top reasons).
5. **Compliance officer:** the compliance dashboard, an exception acknowledged, the
   scheduled-transfer and notification exception kinds, the audit log's hash chain
   verified.
6. **Relationship manager:** customer insights, the customer's details and behavioural profile, an
   outreach call logged; behavioural analytics.
7. **Administrator:** a staff user created, a lockout inspected, the application settings.
8. **Fraud model accuracy:** a page rendered from `docs/ml/model-evaluation-report-v1.md` (the four
   promotion bars, the components and the rules-only fail-safe, the PR curves, the cost curve, SHAP).
9. **Grafana:** API latency and errors, fraud decisions with drift (PSI), availability.

A step whose feature is not on the running build yet (it looks for its page, heading or button first) is
recorded as **PENDING**, takes no video time, and is listed at the end of the run. Release-blocking
recordings must have none.

### One-time setup

```bash
# A scratch venv for the editing tools (no sudo; nothing is committed). imageio-ffmpeg ships a full
# static ffmpeg (H.264, VP9, libass); Playwright's own ffmpeg writes WebM only.
uv venv /tmp/pbip-video-tools --python 3.12
uv pip install --python /tmp/pbip-video-tools/bin/python "imageio-ffmpeg==0.6.0" "markdown-it-py==3.0.0"

cd ~/Desktop/Rachit/pbip/apps/web && npm ci && npx playwright install chromium
```

### Before you record

- The stack is up and seeded (`make seed`). The demo data changes as the walkthrough runs (payees,
  transfers, a loan, a deposit, lab alerts); re-seed afterwards if the data matters.
- Grafana for chapter 9, from the compose file's `monitoring` profile, **without touching the running
  services**: `docker compose --profile monitoring up -d --no-deps grafana loki` (Prometheus is part of the
  same profile and must already be running). Grafana's admin password comes from `GRAFANA_ADMIN_PASSWORD`,
  or else from the Grafana container's own environment; it is typed into the masked field and never printed.
- Leave a minute after any other sign-ins. The run keeps under the limit of 10 sign-in and code requests a
  minute per IP on its own (about 22 in a run), but cannot see other tools' sign-ins.
- Record on a quiet machine: holds are fixed, so a slow page only adds dead time, which the editor cuts.

### Record, then edit

```bash
cd ~/Desktop/Rachit/pbip
OUT=~/Desktop/Rachit/pbip-video/raw
/tmp/pbip-video-tools/bin/python demo/record/render-model-report.py "$OUT"   # writes $OUT/model-report/

cd apps/web
DEMO_VIDEO_DIR="$OUT" npx playwright test -c ../../demo/record/walkthrough.config.ts
# -> $OUT/pbip-walkthrough-<UTC time>.webm and .timeline.json; the run prints PASS/DIFF/PENDING/FAIL

cd ../..
/tmp/pbip-video-tools/bin/python demo/record/edit-walkthrough.py \
  "$OUT"/pbip-walkthrough-<UTC time>.timeline.json --out ~/Desktop/Rachit/pbip-video
```

The editor writes `pbip-walkthrough.mp4` (H.264), `.webm` (VP9), `.chapters.txt` (YouTube style) and `.srt`
(the captions on the edited clock, usable as a voice-over script), and the same four files for
`pbip-walkthrough-highlights`. `--plan-only` prints the lengths without encoding; `--no-webm` skips the slow
VP9 pass; `--highlight-cap` sets the most seconds one step may take in the highlights (default 22).

Environment: `PBIP_DEMO_PASSWORD` (else the `APP_ENV=local` constant in `scripts/seed_dev.py`),
`E2E_COMPOSE_DIR` (the checkout that runs the stack, when recording from a worktree; codes come from
`scripts/totp.py --unused` through `docker compose exec`), `E2E_BASE_URL`, `DEMO_GRAFANA_URL` (default
`http://localhost:3000`), `DEMO_PACE` (scales every hold; default 1), `DEMO_CAPTIONS=0` (no overlay).

### How the edit is made

The recorder writes a timeline next to the raw video: the chapters, every step's start and end (seconds on
the raw clock) with its outcome, detail and highlight flag, and two kinds of marks inside a step:

- **dead**: waiting the viewer does not need (API answers, page loads, reading a code, the sign-in rate
  limit). Cut.
- **fast**: worth seeing but slow (forms filling, a lab scenario running). Played at 1.5x.

Only the time inside steps is used, so sign-outs and the gaps between steps disappear too. Each chapter
starts with a 2-second title card (1.5 seconds in the highlights). The captions are burned into the page
during the recording (an `aria-hidden`, `pointer-events: none` overlay at the bottom) and also written to
the `.srt`. The highlights cut keeps the steps marked `highlight` (MFA sign-in, the step-up transfer, the
fraud lab, the alert with SHAP reasons, the assistant, the dashboards, the model accuracy page), each capped
at its opening third and closing two thirds.

### Checks

Every step reads the API's answer from the browser's own traffic and stops the run with **FAIL** when it is
not one the story allows; any 5xx from the API during a step fails it too. **DIFF** marks an outcome the
demo script documents as an alternative (for example a transfer held instead of a step-up, or the
assistant answering without the language model), and the run continues. The step-up amount is worked out
from Alice's own 90-day transfer history, because every rehearsal moves the `AMOUNT_SPIKE` baseline.
Passwords, codes, keys and tokens are never printed.
