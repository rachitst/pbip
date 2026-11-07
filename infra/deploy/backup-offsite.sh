#!/usr/bin/env bash
# Daily staging backup (runbook 4.1) and its off-host copy to OCI Object Storage (deploy guide section 15).
# Runs as the deploy user from a systemd timer. Needs: ~/pbip (the deployed stack and its .env),
# ~/pbip-repo checked out at the deployed tag with `uv sync`, ~/.pbip-backup-passphrase (mode 600),
# ~/.pbip-oci-s3.env (mode 600) and boto3 in ~/s3-venv. Publishing PostgreSQL on loopback restarts the
# db container, so the availability probe records a few seconds down per run.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd ~/pbip
docker compose -f docker-compose.prod.yml -f docker-compose.prod.backup.yml up -d --wait db >/dev/null
trap 'cd ~/pbip && docker compose -f docker-compose.prod.yml up -d --wait db >/dev/null' EXIT
cd ~/pbip-repo
set -a && . ~/pbip/.env && set +a
export DATABASE_URL="postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:5432/$POSTGRES_DB"
DB=$(docker compose -f ~/pbip/docker-compose.prod.yml ps -q db)
uv run python scripts/backup.py --out-dir /var/backups/pbip --passphrase-file ~/.pbip-backup-passphrase \
  --docker-container "$DB" > /dev/null
dump=$(ls -t /var/backups/pbip/*.dump.gpg | head -1)
~/s3-venv/bin/python scripts/offsite_backup.py put "$dump" "${dump%.dump.gpg}.manifest.json"
# Local copies are kept 14 days; the bucket's own lifecycle rule decides how long the off-host copies live.
find /var/backups/pbip -maxdepth 1 -name 'pbip-*' -mtime +14 -delete
