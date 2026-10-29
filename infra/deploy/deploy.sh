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

start_release() { # <image_prefix> <tag>
  export IMAGE_PREFIX="$1" IMAGE_TAG="$2"
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
