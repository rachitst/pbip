#!/usr/bin/env bash
# One-time GitHub setup with the GitHub CLI (gh). Run AFTER the first push to main:
#   gh auth login
#   scripts/setup_github.sh YOUR_USER/pbip TEAMMATE_USERNAME 2026-10-12
# 1) squash-only merges + auto-delete branches  2) labels  3) sprint milestones  4) main-branch ruleset
# NOTE: rulesets/branch protection on PRIVATE repos need GitHub Pro/Team (or make the repo public).
set -euo pipefail

REPO="${1:?usage: setup_github.sh OWNER/REPO TEAMMATE_USERNAME SPRINT1_START_DATE(YYYY-MM-DD)}"
TEAMMATE="${2:?teammate username required}"
START="${3:?sprint 1 start date required}"

echo "==> Inviting collaborator $TEAMMATE (write access)"
gh api -X PUT "repos/$REPO/collaborators/$TEAMMATE" -f permission=push >/dev/null || echo "   (invite failed - add manually under Settings > Collaborators)"

echo "==> Repository merge settings"
gh repo edit "$REPO" --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false \
  --delete-branch-on-merge --enable-auto-merge

echo "==> Labels"
label() { gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force >/dev/null; }
label "type:feat" 1D76DB "New capability";           label "type:bug" D73A4A "Something is broken"
label "type:chore" CFD3D7 "Maintenance / tooling";   label "type:docs" 0075CA "Documentation"
label "type:security" B60205 "Security-relevant";    label "type:test" 0E8A16 "Tests only"
label "area:api" 5319E7 "Backend";                    label "area:web" FBCA04 "Frontend"
label "area:ml" 0E8A16 "Data / ML";                   label "area:infra" 006B75 "CI / Docker / ops"
label "tier:must" B60205 "Must have";                 label "tier:should" D93F0B "Should have"
label "tier:could" FEF2C0 "Could have";               label "blocked" 000000 "Waiting on something"

echo "==> Sprint milestones (7 x 2 weeks from $START)"
python3 - "$REPO" "$START" <<'PY'
import datetime, subprocess, sys
repo, start = sys.argv[1], datetime.date.fromisoformat(sys.argv[2])
for n in range(1, 8):
    due = start + datetime.timedelta(weeks=2 * n) - datetime.timedelta(days=1)
    subprocess.run(["gh", "api", f"repos/{repo}/milestones", "-f", f"title=Sprint {n}",
                    "-f", f"due_on={due.isoformat()}T23:59:59Z"], check=False, capture_output=True)
print("   created Sprint 1..7")
PY

echo "==> Branch ruleset for main"
if gh api -X POST "repos/$REPO/rulesets" --input .github/rulesets/main.json >/dev/null; then
  echo "   ruleset 'protect-main' created"
else
  echo "   FAILED. Private repo on a free plan? Make the repo public or get GitHub Pro (Student Pack)."
  echo "   Or import .github/rulesets/main.json manually: Settings > Rules > Rulesets > New > Import."
fi
echo "Done. Next: Settings > Code security > enable Dependabot alerts (+ secret scanning if available)."
