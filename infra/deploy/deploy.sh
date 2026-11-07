#!/usr/bin/env bash
# Health-gated deployment of the PBIP stack on the staging host, with rollback to the previous release.
#
#   bash deploy.sh deploy <image_prefix> <tag>   pull, run migrations, start, wait for health checks;
#                                                records the tag only once the stack is healthy
#   bash deploy.sh rollback                      redeploy the previously recorded healthy release
#   bash deploy.sh monitoring                    start or update Prometheus, Grafana, Loki and the exporters
#                                                (docker-compose.prod.monitoring.yml, its own compose project;
#                                                ADR-0059, runbook section 1.5)
#
# Run from ~/pbip next to docker-compose.prod.yml and the host .env (infra/deploy/.env.prod.example).
# State files next to this script: .deployed_tag (current healthy release), .previous_tag (the one
# before it), .deployed_tag.pending (a deploy in progress). Procedures: docs/operations/runbook.md
# section 2. Migrations run before the API starts (compose dependency) and are never downgraded here.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

COMPOSE=(docker compose -f docker-compose.prod.yml)
MONITORING=(docker compose -f docker-compose.prod.monitoring.yml)
STATE_CURRENT=.deployed_tag
STATE_PREVIOUS=.previous_tag
STATE_PENDING=.deployed_tag.pending
WAIT_TIMEOUT="${DEPLOY_WAIT_TIMEOUT:-180}"

env_value() { # <name>: a value from the host .env (compose reads the same file); empty if unset
  [ -f .env ] || return 0
  sed -n "s/^$1=//p" .env | tail -n 1
}

# TLS material the stack refuses to start without (runbook 1.4). Never overwrites existing files:
#   certs/db   self-signed PostgreSQL certificate; also the API's sslrootcert (sslmode=verify-full)
#   certs/web  placeholder self-signed certificate for PUBLIC_HOSTNAME, so nginx can start and answer
#              the first certbot HTTP-01 challenge; certbot-deploy-hook.sh replaces it with the real one
ensure_tls() {
  local host
  host="$(env_value PUBLIC_HOSTNAME)"
  mkdir -p acme certs/db certs/web
  chmod 0700 certs
  chmod 0755 certs/db certs/web acme # mounted into containers that run as non-root users
  if [ ! -s certs/db/server.crt ] || [ ! -s certs/db/server.key ]; then
    echo "==> Creating the PostgreSQL TLS certificate (certs/db)"
    # MSYS_NO_PATHCONV=1: Git Bash on Windows rewrites the argument "/CN=db" into a Windows path
    # ("C:/Program Files/Git/CN=db") before a native openssl sees it, and the call fails. Ignored on
    # Linux and macOS (#329).
    MSYS_NO_PATHCONV=1 openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
      -subj "/CN=db" -addext "subjectAltName=DNS:db" \
      -keyout certs/db/server.key -out certs/db/server.crt 2>/dev/null
    chmod 0600 certs/db/server.key # copied and re-owned by tls-entrypoint.sh
  fi
  if [ ! -s certs/web/tls.crt ] || [ ! -s certs/web/tls.key ]; then
    echo "==> Creating a placeholder web certificate for ${host:-localhost}; run certbot next (runbook 1.4)"
    MSYS_NO_PATHCONV=1 openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 30 \
      -subj "/CN=${host:-localhost}" -addext "subjectAltName=DNS:${host:-localhost}" \
      -keyout certs/web/tls.key -out certs/web/tls.crt 2>/dev/null
    chmod 0644 certs/web/tls.key # nginx-unprivileged (uid 101) reads it; certs/ itself is 0700
  fi
  chmod 0755 tls-entrypoint.sh 2>/dev/null || true
}

# A secret the monitoring stack needs, generated on the host (python3 -c "import secrets;
# print(secrets.token_urlsafe(18))"). Refuses an unset, placeholder, short or known default value, and anything
# outside the URL-safe alphabet (the value is written into SQL below, so the alphabet is also the escaping).
require_generated_secret() { # <name>
  local value
  value="$(env_value "$1")"
  if ! [[ "$value" =~ ^[A-Za-z0-9_-]{20,}$ ]] || [ "$value" = admin_local_password ]; then
    echo "refusing to start monitoring: $1 in .env must be a generated secret of at least 20 URL-safe" \
      "characters (runbook section 1.5)" >&2
    exit 4
  fi
}

# The postgres-exporter's login: member of pg_monitor (read-only statistics), never the application owner.
# Idempotent; the password goes over stdin, never on a command line.
ensure_monitor_role() {
  local role owner db password
  role="$(env_value POSTGRES_MONITOR_USER)"
  role="${role:-pbip_monitor}"
  owner="$(env_value POSTGRES_USER)"
  db="$(env_value POSTGRES_DB)"
  password="$(env_value POSTGRES_MONITOR_PASSWORD)"
  if ! [[ "$role" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || [ "$role" = "$owner" ]; then
    echo "refusing to start monitoring: POSTGRES_MONITOR_USER must be a lower-case role name other than" \
      "POSTGRES_USER" >&2
    exit 4
  fi
  echo "==> Ensuring the read-only PostgreSQL role $role (pg_monitor)"
  "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -q -U "$owner" -d "$db" <<SQL
SELECT 'CREATE ROLE $role' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$role')\gexec
ALTER ROLE $role WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 3 PASSWORD '$password';
GRANT pg_monitor TO $role;
SQL
}

# The compose secrets of docker-compose.prod.monitoring.yml: ./secrets/<file> from the .env value, without a
# newline. secrets/ is 0700, so on the host only this user (and root) can read them; the files themselves are 0644
# because they are bind-mounted into containers that run as other users (Grafana uid 472, the exporter nobody),
# and a bind mount does not pass through the host directory. Written in place, so a running container sees the
# new value; prints "changed" when it replaces a different earlier value.
write_secret_file() { # <env name> <file>
  local value old=""
  value="$(env_value "$1")"
  mkdir -p secrets
  chmod 0700 secrets
  if [ -f "secrets/$2" ]; then
    old="$(cat "secrets/$2")"
  fi
  (umask 077 && printf '%s' "$value" > "secrets/$2")
  chmod 0644 "secrets/$2"
  if [ -n "$old" ] && [ "$old" != "$value" ]; then
    echo changed
  fi
}

start_monitoring() {
  if [ ! -f .env ]; then
    echo "refusing to start monitoring: no .env next to deploy.sh" >&2
    exit 4
  fi
  require_generated_secret GRAFANA_ADMIN_PASSWORD
  require_generated_secret POSTGRES_MONITOR_PASSWORD
  ensure_monitor_role
  local exporter_password_changed
  write_secret_file GRAFANA_ADMIN_PASSWORD grafana_admin_password > /dev/null
  exporter_password_changed="$(write_secret_file POSTGRES_MONITOR_PASSWORD postgres_monitor_password)"
  echo "==> Starting the monitoring stack (project pbip-prod-monitoring; Grafana and Prometheus on 127.0.0.1 only)"
  "${MONITORING[@]}" pull --quiet
  "${MONITORING[@]}" up -d --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT"
  if [ -n "$exporter_password_changed" ]; then
    # The exporter reads its password file once, at start.
    "${MONITORING[@]}" restart postgres-exporter
  fi
  "${MONITORING[@]}" ps
}

start_release() { # <image_prefix> <tag>
  export IMAGE_PREFIX="$1" IMAGE_TAG="$2"
  ensure_tls
  echo "==> Pulling $1/api:$2 and $1/web:$2"
  "${COMPOSE[@]}" pull --quiet
  echo "==> Starting release $2 (migrate first, then waiting up to ${WAIT_TIMEOUT}s for health checks)"
  "${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT"
  "${COMPOSE[@]}" ps
}

case "${1:-}" in
  deploy)
    prefix="${2:?usage: deploy.sh deploy <image_prefix> <tag>}"
    tag="${3:?usage: deploy.sh deploy <image_prefix> <tag>}"
    if ! [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "refusing to deploy '$tag': only release tags vX.Y.Z are deployed" >&2
      exit 2
    fi
    if [ -f "$STATE_CURRENT" ]; then
      cp "$STATE_CURRENT" "$STATE_PREVIOUS"
    fi
    printf '%s %s\n' "$prefix" "$tag" > "$STATE_PENDING"
    start_release "$prefix" "$tag"
    mv "$STATE_PENDING" "$STATE_CURRENT"
    echo "==> Release $tag is up and healthy"
    ;;
  rollback)
    if [ ! -f "$STATE_PREVIOUS" ]; then
      echo "no previous healthy release is recorded; nothing to roll back to" >&2
      exit 3
    fi
    read -r prefix tag < "$STATE_PREVIOUS"
    echo "==> Rolling back to $tag"
    start_release "$prefix" "$tag"
    printf '%s %s\n' "$prefix" "$tag" > "$STATE_CURRENT"
    rm -f "$STATE_PENDING"
    echo "==> Rolled back to $tag (database migrations are not downgraded automatically)"
    ;;
  monitoring)
    start_monitoring
    ;;
  *)
    echo "usage: deploy.sh deploy <image_prefix> <tag> | deploy.sh rollback | deploy.sh monitoring" >&2
    exit 64
    ;;
esac
