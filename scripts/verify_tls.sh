#!/usr/bin/env bash
# Prove that the running compose stack enforces TLS (NFR-02). Run after `make up`; CI runs it in the
# e2e job. Exits non-zero on the first failed check.
#
#   bash scripts/verify_tls.sh [https_url] [http_url]     defaults: https://localhost:8443 http://localhost:8080
#   COMPOSE="docker compose -p other" bash scripts/verify_tls.sh ...   for a non-default compose project
set -euo pipefail

HTTPS_URL="${1:-https://localhost:8443}"
HTTP_URL="${2:-http://localhost:8080}"
CA="${CA:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra/certs/ca.crt}"
read -r -a COMPOSE <<< "${COMPOSE:-docker compose}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok:   $*"; }

# 1. Plain HTTP only redirects, to the same path on HTTPS.
headers="$(curl -sS -o /dev/null -D - "$HTTP_URL/accounts?x=1")"
grep -qE '^HTTP/[0-9.]+ 301' <<< "$headers" || fail "HTTP did not answer 301"
grep -qi "^location: ${HTTPS_URL}/accounts?x=1" <<< "$headers" || fail "HTTP redirect target is not ${HTTPS_URL}/accounts?x=1"
pass "HTTP redirects to HTTPS (301, path and query kept)"
api_http="$(curl -sS -o /dev/null -w '%{http_code}' "$HTTP_URL/api/health/live")"
[ "$api_http" = "301" ] || fail "the API answered over plain HTTP ($api_http)"
pass "the API is not served over plain HTTP"

# 2. HTTPS with a chain that verifies against the CA, and HSTS on static and API responses.
for path in / /api/health/live; do
  h="$(curl -sS --cacert "$CA" -o /dev/null -D - "$HTTPS_URL$path")" || fail "TLS handshake/verification failed for $path"
  grep -qi '^strict-transport-security: max-age=63072000; includeSubDomains' <<< "$h" || fail "no HSTS on $path"
done
pass "HTTPS verifies against the CA; HSTS on static and API responses"

# 3. Old protocols are refused (TLS 1.2 minimum).
if curl -sS --cacert "$CA" --tls-max 1.1 -o /dev/null "$HTTPS_URL/" 2>/dev/null; then
  fail "a TLS 1.1 handshake succeeded"
fi
pass "TLS 1.1 and older refused"

# 4. PostgreSQL: plain TCP rejected, TLS accepted, and the API's own connections are encrypted.
pg() { "${COMPOSE[@]}" exec -T db sh -c "PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -XAtq \"host=127.0.0.1 user=\$POSTGRES_USER dbname=\$POSTGRES_DB $1\" -c \"$2\""; }
if pg "sslmode=disable" "select 1" >/dev/null 2>&1; then
  fail "PostgreSQL accepted a plain-TCP connection"
fi
pass "PostgreSQL rejects plain TCP"
[ "$(pg "sslmode=require" "select ssl from pg_stat_ssl where pid = pg_backend_pid()")" = "t" ] || fail "TLS connection to PostgreSQL failed"
pass "PostgreSQL accepts TLS"
plain="$(pg "sslmode=require" "select count(*) from pg_stat_activity a join pg_stat_ssl s using (pid) where a.client_addr is not null and not s.ssl")"
encrypted="$(pg "sslmode=require" "select count(*) from pg_stat_activity a join pg_stat_ssl s using (pid) where a.client_addr is not null and s.ssl and a.pid <> pg_backend_pid()")"
[ "$plain" = "0" ] || fail "$plain unencrypted client connection(s) to PostgreSQL"
[ "$encrypted" -ge 1 ] || fail "no encrypted application connections to PostgreSQL (is the API up?)"
pass "all $encrypted application connection(s) to PostgreSQL use TLS"

echo "TLS verification passed for $HTTPS_URL"
