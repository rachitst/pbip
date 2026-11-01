#!/usr/bin/env bash
# certbot --deploy-hook for the staging host: install a newly issued/renewed certificate for nginx and
# reload it without downtime. certbot runs it as root with RENEWED_LINEAGE=/etc/letsencrypt/live/<host>.
#
#   sudo certbot certonly --webroot -w /home/<deploy>/pbip/acme -d <host> \
#     --deploy-hook "PBIP_DIR=/home/<deploy>/pbip bash /home/<deploy>/pbip/certbot-deploy-hook.sh"
#
# Renewals reuse the hook (certbot stores it in the renewal config). Procedure: runbook section 1.4.
set -euo pipefail

: "${RENEWED_LINEAGE:?certbot sets RENEWED_LINEAGE; run this only as a certbot deploy hook}"
PBIP_DIR="${PBIP_DIR:?set PBIP_DIR to the directory holding docker-compose.prod.yml}"
owner="$(stat -c '%u:%g' "$PBIP_DIR")"

install -d -o "${owner%:*}" -g "${owner#*:}" -m 0755 "$PBIP_DIR/certs/web"
# 0644: nginx-unprivileged (uid 101) reads the key through a bind mount; $PBIP_DIR/certs is 0700.
install -o "${owner%:*}" -g "${owner#*:}" -m 0644 "$RENEWED_LINEAGE/fullchain.pem" "$PBIP_DIR/certs/web/tls.crt"
install -o "${owner%:*}" -g "${owner#*:}" -m 0644 "$RENEWED_LINEAGE/privkey.pem" "$PBIP_DIR/certs/web/tls.key"

cd "$PBIP_DIR"
docker compose -f docker-compose.prod.yml exec -T web nginx -t
docker compose -f docker-compose.prod.yml exec -T web nginx -s reload
echo "certbot-deploy-hook: installed $(basename "$RENEWED_LINEAGE") and reloaded nginx"
