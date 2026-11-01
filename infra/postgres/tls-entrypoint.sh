#!/bin/sh
# PostgreSQL with TLS required for every TCP connection (NFR-02, encryption in transit).
#
# The certificate pair is mounted read-only at /etc/pbip-db-certs/server.{crt,key}: scripts/dev_certs.py
# locally, `deploy.sh` on the staging host (runbook 1.4). PostgreSQL refuses a key it cannot own with
# 0600, and a bind mount keeps the host's owner, so the pair is copied into the container and re-owned
# before the stock entrypoint (which drops to the postgres user) starts the server.
#
# pg_hba: the in-container socket keeps the image's default (trust: healthcheck and init scripts),
# TCP is accepted ONLY over TLS with SCRAM; plain TCP is rejected outright.
set -eu

SRC=/etc/pbip-db-certs
DST=/var/run/pbip-db-tls

for f in server.crt server.key; do
  if [ ! -r "$SRC/$f" ]; then
    echo "tls-entrypoint: $SRC/$f is missing; run 'python scripts/dev_certs.py' (dev) or deploy.sh (staging)" >&2
    exit 1
  fi
done

install -d -o postgres -g postgres -m 0700 "$DST"
install -o postgres -g postgres -m 0644 "$SRC/server.crt" "$DST/server.crt"
install -o postgres -g postgres -m 0600 "$SRC/server.key" "$DST/server.key"
cat > "$DST/pg_hba.conf" <<'HBA'
# TYPE   DATABASE  USER  ADDRESS  METHOD
local    all       all            trust
hostssl  all       all   all      scram-sha-256
host     all       all   all      reject
HBA
chown postgres:postgres "$DST/pg_hba.conf"
chmod 0600 "$DST/pg_hba.conf"

exec docker-entrypoint.sh "$@" \
  -c ssl=on \
  -c ssl_min_protocol_version=TLSv1.2 \
  -c "ssl_cert_file=$DST/server.crt" \
  -c "ssl_key_file=$DST/server.key" \
  -c "hba_file=$DST/pg_hba.conf"
