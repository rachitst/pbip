#!/usr/bin/env bash
# One availability probe of the staging stack (TASK-103); a systemd timer runs it every 30 s.
#
#   bash uptime-probe.sh <url> <logfile>
#   e.g. bash uptime-probe.sh https://<host>/api/health/ready ~/pbip/uptime/probe.log
#
# Appends one line: "<UTC time> <up|down> <HTTP status, 000 = no response> <seconds>". Up means HTTP 200
# within PROBE_TIMEOUT seconds (default 10); redirects are not followed. The TLS chain and host name are
# verified (PROBE_CACERT=<file> trusts a private CA instead of the system store). PROBE_CONNECT_TO is
# passed to curl --connect-to, so "::127.0.0.1:" probes nginx on this host under the public name.
# scripts/uptime_report.py turns the log into the availability figure. Procedure:
# docs/operations/deploy-oracle-cloud-arm.md section 17.
set -euo pipefail

url="${1:?usage: uptime-probe.sh <url> <logfile>}"
log="${2:?usage: uptime-probe.sh <url> <logfile>}"

args=(--silent --output /dev/null --max-time "${PROBE_TIMEOUT:-10}" --write-out '%{http_code} %{time_total}')
if [ -n "${PROBE_CACERT:-}" ]; then
  args+=(--cacert "$PROBE_CACERT")
fi
if [ -n "${PROBE_CONNECT_TO:-}" ]; then
  args+=(--connect-to "$PROBE_CONNECT_TO")
fi

at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# curl prints "000 <time>" and exits non-zero when nothing answered; that is a "down" result, not an error.
result="$(curl "${args[@]}" "$url" 2>/dev/null)" || true
code="${result%% *}"
seconds="${result#* }"
if ! [[ "$code" =~ ^[0-9]{3}$ ]]; then
  code=000
fi
if ! [[ "$seconds" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  seconds=0
fi
state=down
if [ "$code" = "200" ]; then
  state=up
fi

mkdir -p "$(dirname "$log")"
printf '%s %s %s %s\n' "$at" "$state" "$code" "$seconds" >> "$log"
