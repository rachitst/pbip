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
# ML_MODELS=enabled in .env adds docker-compose.prod.models.yml (the trained fraud and recommendation models,
# ADR-0061, deploy guide section 14.1) to deploy and rollback alike; without it the stack serves the rules.
# State files next to this script: .deployed_tag (current healthy release), .previous_tag (the one
# before it), .deployed_tag.pending (a deploy in progress). Procedures: docs/operations/runbook.md
# section 2. Migrations run before the API starts and are never downgraded here.
#
# When api replicas are already running, a release replaces them rolling (ADR-0062, #505): the new replicas
# start beside the old ones, must pass their health checks, and only then are the old ones stopped one at a
# time (uvicorn finishes its in-flight requests) with an nginx reload after each, so nginx never routes to a
# stopped replica. A new release that never turns healthy is removed and the running one keeps serving.
# The web container (nginx) is still recreated, which is the only gap left; runbook 2.1 records its size.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

COMPOSE=(docker compose -f docker-compose.prod.yml)
MODELS_OVERLAY=docker-compose.prod.models.yml
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

# The opt-in model overlay (ADR-0061). With ML_MODELS=enabled the release must find the operator-copied model
# files: a missing directory would otherwise be created empty by Docker (owned by root) and the API would quietly
# serve the rules, so the deploy stops before anything changes. Any other value than enabled/disabled/unset is
# refused for the same reason (a typo must not silently mean "off").
select_models() {
  local mode missing=()
  mode="$(env_value ML_MODELS)"
  case "$mode" in
    "" | disabled) return 0 ;;
    enabled) ;;
    *)
      echo "refusing to deploy: ML_MODELS in .env must be enabled, disabled or unset" >&2
      exit 5
      ;;
  esac
  [ -f "$MODELS_OVERLAY" ] || missing+=("$MODELS_OVERLAY")
  [ -f models/fraud/manifest.json ] || missing+=(models/fraud/manifest.json)
  [ -d models/recommendations ] || missing+=(models/recommendations/)
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "refusing to deploy with ML_MODELS=enabled: missing ${missing[*]} next to deploy.sh" \
      "(copy the models first, or set ML_MODELS=disabled; deploy guide section 14.1)" >&2
    exit 5
  fi
  echo "==> Trained models enabled (ML_MODELS=enabled): mounting models/fraud and models/recommendations read-only"
  COMPOSE+=(-f "$MODELS_OVERLAY")
}

# Waits until every given container reports healthy; fails on "unhealthy", on a container that is gone, or after
# WAIT_TIMEOUT seconds.
wait_healthy() { # <container id>...
  local deadline=$((SECONDS + WAIT_TIMEOUT)) id status waiting
  while :; do
    waiting=0
    for id in "$@"; do
      status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" 2>/dev/null)" ||
        status=gone
      case "$status" in
        healthy) ;;
        starting) waiting=1 ;;
        *)
          echo "new api container ${id:0:12} is $status" >&2
          return 1
          ;;
      esac
    done
    [ "$waiting" = 0 ] && return 0
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "new api replicas not healthy within ${WAIT_TIMEOUT}s" >&2
      return 1
    fi
    sleep 2
  done
}

# nginx re-resolves "api" only every 10 s (default.conf.template); a reload starts workers that resolve it now,
# while the old workers finish their in-flight requests.
reload_web() {
  "${COMPOSE[@]}" exec -T web nginx -s reload ||
    echo "warning: could not reload nginx in web; it re-resolves the api within 10 s" >&2
}

roll_api() { # <running api container id>...
  local old=("$@") all=() new=() replicas id
  replicas="$(env_value API_REPLICAS)"
  replicas="${replicas:-2}"
  if ! [[ "$replicas" =~ ^[1-9][0-9]?$ ]]; then
    echo "refusing to deploy: API_REPLICAS in .env must be a number from 1 to 99" >&2
    exit 5
  fi
  echo "==> Starting $replicas new api replicas beside the ${#old[@]} running ones (rolling, ADR-0062)"
  "${COMPOSE[@]}" up -d --no-deps --no-recreate --scale "api=$((${#old[@]} + replicas))" api
  mapfile -t all < <("${COMPOSE[@]}" ps -q api)
  for id in "${all[@]}"; do
    [[ " ${old[*]} " == *" $id "* ]] || new+=("$id")
  done
  if [ "${#new[@]}" -ne "$replicas" ]; then
    echo "expected $replicas new api containers, found ${#new[@]}" >&2
    [ "${#new[@]}" -eq 0 ] || docker rm -f "${new[@]}" > /dev/null
    return 1
  fi
  echo "==> Waiting up to ${WAIT_TIMEOUT}s for the new replicas' health checks"
  if ! wait_healthy "${new[@]}"; then
    echo "==> Removing the new replicas; the running release keeps serving" >&2
    docker rm -f "${new[@]}" > /dev/null
    return 1
  fi
  for id in "${old[@]}"; do
    echo "==> Retiring api ${id:0:12}"
    docker stop -t 30 "$id" > /dev/null # SIGTERM: uvicorn stops accepting and finishes in-flight requests
    reload_web                           # first: until then a worker may still route to the stopped address
    docker rm "$id" > /dev/null
  done
}

start_release() { # <image_prefix> <tag>
  export IMAGE_PREFIX="$1" IMAGE_TAG="$2"
  local running=()
  ensure_tls
  echo "==> Pulling $1/api:$2 and $1/web:$2"
  "${COMPOSE[@]}" pull --quiet
  mapfile -t running < <("${COMPOSE[@]}" ps -q api)
  if [ "${#running[@]}" -eq 0 ]; then
    echo "==> Starting release $2 (migrate first, then waiting up to ${WAIT_TIMEOUT}s for health checks)"
    "${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT"
  else
    echo "==> Rolling release $2: migrations first, while the running api keeps serving"
    "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" db redis
    "${COMPOSE[@]}" up --no-deps --exit-code-from migrate migrate
    roll_api "${running[@]}"
    echo "==> Replacing worker, beat and web (waiting up to ${WAIT_TIMEOUT}s for health checks)"
    "${COMPOSE[@]}" up -d --no-deps --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT" worker beat web
  fi
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
    select_models
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
    select_models
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
