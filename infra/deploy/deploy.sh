#!/usr/bin/env bash
# Health-gated deployment of the PBIP stack on the staging host, with rollback to the previous release.
#
#   bash deploy.sh deploy <image_prefix> <tag>   pull, run migrations, start, wait for health checks;
#                                                records the tag only once the stack is healthy
#   bash deploy.sh rollback                      redeploy the previously recorded healthy release
#
# Run from ~/pbip next to docker-compose.prod.yml and the host .env (infra/deploy/.env.prod.example).
# State files next to this script: .deployed_tag (current healthy release), .previous_tag (the one
# before it), .deployed_tag.pending (a deploy in progress). Procedures: docs/operations/runbook.md
# section 2. Migrations run before the API starts (compose dependency) and are never downgraded here.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

COMPOSE=(docker compose -f docker-compose.prod.yml)
STATE_CURRENT=.deployed_tag
STATE_PREVIOUS=.previous_tag
STATE_PENDING=.deployed_tag.pending
WAIT_TIMEOUT="${DEPLOY_WAIT_TIMEOUT:-180}"

env_value() { # <name>: a value from the host .env (compose reads the same file)
  sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1
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
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
      -subj "/CN=db" -addext "subjectAltName=DNS:db" \
      -keyout certs/db/server.key -out certs/db/server.crt 2>/dev/null
    chmod 0600 certs/db/server.key # copied and re-owned by tls-entrypoint.sh
  fi
  if [ ! -s certs/web/tls.crt ] || [ ! -s certs/web/tls.key ]; then
    echo "==> Creating a placeholder web certificate for ${host:-localhost}; run certbot next (runbook 1.4)"
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 30 \
      -subj "/CN=${host:-localhost}" -addext "subjectAltName=DNS:${host:-localhost}" \
      -keyout certs/web/tls.key -out certs/web/tls.crt 2>/dev/null
    chmod 0644 certs/web/tls.key # nginx-unprivileged (uid 101) reads it; certs/ itself is 0700
  fi
  chmod 0755 tls-entrypoint.sh 2>/dev/null || true
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
  *)
    echo "usage: deploy.sh deploy <image_prefix> <tag> | deploy.sh rollback" >&2
    exit 64
    ;;
esac
