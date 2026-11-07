# ADR 0059: Monitoring for the production compose project, as its own project on the production network
- Status: Accepted (under owner's standing approval, 2026-11-08)
- Date: 2026-11-08
- Deciders: Architecture Team (follows TASK-069, TASK-103, ADR-0045)

## Context
Prometheus, Grafana, Loki, Alloy, the blackbox exporter and the Celery, Redis and PostgreSQL exporters exist only
as the `monitoring` profile of the local `docker-compose.yml`. The staging host (Oracle Cloud Arm VM, 2 OCPU,
12 GB) runs `infra/deploy/docker-compose.prod.yml` as compose project `pbip-prod`, which has no monitoring, so
the dashboards, the 20 alert and recording rules and the monthly availability figure (`web-ready`) were not
available where they matter. The deploy guide (section 16) recorded this as an open engineering change.

Constraints: nothing new may be published to the internet; no default credentials in a deployed environment;
images must run on linux/arm64; the stack must fit next to the production services; dashboards, rules and the
scrape configuration must not be forked; a production release must keep working exactly as it does.

## Decision
1. **`infra/deploy/docker-compose.prod.monitoring.yml`, a separate compose project `pbip-prod-monitoring`**
   that joins `pbip-prod_default` as an external network, started by `bash deploy.sh monitoring`. Not a `-f`
   overlay of the `pbip-prod` project: `deploy.sh deploy` runs `up --remove-orphans` with the production file
   only, so in the same project every release would remove the monitoring containers as orphans, and adding
   the overlay to the release's `up --wait` would let a monitoring fault fail (and roll back) a release.
2. **The repository's own configuration, unforked.** The production service names (`api`, `web`, `redis`,
   `db`, `worker`) equal the local ones, so `infra/prometheus/prometheus.yml` (every API replica through the DNS
   name `api`, ADR-0045), `alerts.yml`, the Grafana provisioning and dashboards, `loki.yml` and
   `config.alloy` are mounted as they are. The deploy job copies `infra/{prometheus,grafana,loki,alloy}` to
   `~/pbip/monitoring/` on every release; mounts are directories, so new files are visible to running
   containers. Production-only differences are command-line flags or environment, not copies:
   * blackbox: the shared `blackbox.yml` verifies nginx's certificate for the name `web` (local dev CA); the
     container renders it with `PUBLIC_HOSTNAME` (validated as a host name or IPv4 address) and mounts the
     host's CA bundle (or `MONITORING_PROBE_CA_FILE` for option B's private CA), so `web-ready` proves the
     real certificate through nginx;
   * Loki: 7-day retention by flags (`-compactor.retention-enabled`, `-store.retention=168h`);
   * Alloy: `config.alloy` takes the Docker endpoint from `DOCKER_HOST` and falls back to the socket (local
     behaviour unchanged).
3. **Security.**
   * Only Grafana (`127.0.0.1:3000`) and Prometheus (`127.0.0.1:9090`) publish ports, on loopback; operators
     use an SSH tunnel over the existing key-only, IP-restricted port 22. The security list is unchanged.
   * `GRAFANA_ADMIN_PASSWORD` has no default: compose refuses to start without it (`:?`) and `deploy.sh
     monitoring` refuses anything shorter than 20 URL-safe characters, a `<placeholder>` or the local example
     value. Sign-up, anonymous access, external snapshots, usage reporting, update checks, the news feed and
     plugin pre-installation are off, so Grafana sends nothing off the host.
   * The PostgreSQL exporter logs in as `pbip_monitor`, a member of `pg_monitor` with `CONNECTION LIMIT 3`,
     not the application owner; `deploy.sh monitoring` creates it idempotently over the database's local
     socket, password on stdin only. TLS `verify-full` against the database certificate, like the API.
   * Alloy does not mount the Docker socket (socket access is root on the host). It reads containers through
     `tecnativa/docker-socket-proxy`, which allows GET on `/containers`, `/networks`, `/_ping` and `/version`
     only and refuses every write; the proxy is on an `internal` network shared with Alloy alone, so neither
     the production containers nor the exporters can reach it. Alloy runs as its unprivileged user.
   * Every container except the proxy runs with `cap_drop: [ALL]` and `no-new-privileges`; the blackbox
     exporter runs as `nobody`. Exporters and the blackbox exporter are on the production network only;
     Grafana and Loki on the monitoring network only.
4. **Images**: the local profile's versions, pinned by the digest of the multi-arch manifest list; each list
   was inspected with `docker buildx imagetools inspect` and includes linux/arm64. The Docker Hub
   `danihodovic/celery-exporter:0.12.2` is amd64-only, so the overlay uses the project's own GHCR build of the
   same release (`ghcr.io/danihodovic/celery-exporter:0.12.2`, amd64 and arm64, same entrypoint and build date).
5. **Resources**: memory and CPU limits on every service, 3 GiB and 3.0 CPUs in total at most (Prometheus 1 GiB,
   Grafana and Loki 512 MiB, Alloy and celery-exporter 256 MiB, the others 64-128 MiB). Prometheus keeps
   35 days (the monthly availability figure) and at most 4 GB; Loki keeps 7 days. Measured idle on the laptop
   against the local stack: about 360 MiB in total.
6. **The production worker runs with `-E`** (task events), like the local one, so `celery_task_*` metrics and
   the `CeleryTaskFailures` alert work. Worker heartbeats (`celery_worker_up`) are sent without it.

## Alternatives considered
* **A `-f` overlay in the `pbip-prod` project** (the first idea) - removed by every release's
  `--remove-orphans`, or coupled to the release's health gate; see Decision 1.
* **A production copy of `prometheus.yml`, `blackbox.yml` and the dashboards** - drifts from the local stack
  that the tests and the demo exercise; the service names are the same, so there is nothing to change.
* **Mount the Docker socket into Alloy, as the local profile does** - acceptable on a laptop, but on the
  staging host it would give a log shipper root on the machine; the proxy costs one 11 MiB container.
* **Exporter with the application owner's credentials, as locally** - one more container holding a password
  that can read and change every row; `pg_monitor` is enough for every panel and alert.
* **Alertmanager with a notification channel** - where pages go is an owner decision (runbook 1.5); this ADR
  makes alerts visible on staging and does not send anything off the host.
* **An nginx exporter** - needs `stub_status` in the web image; nginx is covered by the `web-ready` probe
  through TLS, and its errors reach Loki. Left for a separate change if connection-level metrics are needed.

## Consequences
* Staging gets the same dashboards, alerts and availability figure as the local stack, reached through an SSH
  tunnel. Nothing new is reachable from the internet; nothing is sent to a third party.
* Operators run `bash deploy.sh monitoring` once, and again after a release that changes its configuration
  (or send Prometheus `SIGHUP`); Grafana reloads dashboards by itself.
* `docker compose -f docker-compose.prod.yml down` cannot remove `pbip-prod_default` while monitoring is
  attached: stop monitoring first. If the production network is recreated, re-run `deploy.sh monitoring`.
* New attack surface, recorded in the threat model (T72): the Docker API proxy, a monitoring database role and
  two loopback services. Residual: anyone with a shell on the host can reach the loopback ports (they are also
  in the `docker` group, i.e. root); Grafana's admin password is read only on its first start, so rotation is
  `grafana cli admin reset-admin-password --password-from-stdin`.
* Standing-approval conditions: no weakening of security, auth or audit (the change adds controls and
  publishes nothing); no money path; no migration (the monitoring role is cluster-level, not schema, and has no
  data); no new external service or paid dependency and no data sent outside the stack (all images are
  self-hosted open source, Grafana's outbound features are off); no fake metrics; within the requested scope.
