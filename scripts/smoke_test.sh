#!/usr/bin/env bash
# Post-deploy smoke test.   scripts/smoke_test.sh http://localhost:8080 [--api-only]
set -euo pipefail

BASE="${1:?usage: smoke_test.sh <base-url> [--api-only]}"
BASE="${BASE%/}"
API_ONLY="${2:-}"
failures=0

check() { # name expected-status actual-status
  if [ "$2" = "$3" ]; then echo "  ok   $1 ($3)"; else echo "  FAIL $1: expected $2, got $3"; failures=$((failures + 1)); fi
}
status() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }

echo "Waiting for readiness (up to 90s)..."
for _ in $(seq 1 45); do
  [ "$(status "$BASE/api/health/ready")" = "200" ] && break
  sleep 2
done

echo "Smoke test against $BASE"
check "readiness"                      200 "$(status "$BASE/api/health/ready")"
check "unauthenticated /accounts"      401 "$(status "$BASE/api/v1/accounts")"
check "bad credentials rejected"       401 "$(status -X POST -H 'Content-Type: application/json' \
  -d '{"email":"nobody@example.com","password":"definitely-wrong-123"}' "$BASE/api/v1/auth/login")"
check "transfer without auth"          401 "$(status -X POST -H 'Content-Type: application/json' -d '{}' "$BASE/api/v1/transfers")"

hdrs="$(curl -s -D - -o /dev/null --max-time 10 "$BASE/api/health/live")"
echo "$hdrs" | grep -qi '^x-content-type-options: nosniff' && echo "  ok   API security headers" || { echo "  FAIL API security headers"; failures=$((failures + 1)); }

if [ "$API_ONLY" != "--api-only" ]; then
  check "web app served"               200 "$(status "$BASE/")"
  check "metrics not exposed publicly" 404 "$(status "$BASE/metrics")"
  curl -s --max-time 10 "$BASE/" | grep -q 'PBIP' && echo "  ok   web page content" || { echo "  FAIL web page content"; failures=$((failures + 1)); }
fi

if [ "$failures" -gt 0 ]; then echo "SMOKE TEST FAILED ($failures)"; exit 1; fi
echo "SMOKE TEST PASSED"
