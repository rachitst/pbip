# ADR 0062: Rolling replacement of the api replicas on deploy
- Status: Accepted (under owner's standing approval, 2026-11-08)
- Date: 2026-11-08
- Deciders: Architecture Team (follows ADR-0045; TASK-082 acceptance criterion 3, #505)

## Context
TASK-082's third acceptance criterion asks for a deploy that "starts the stack with zero downtime". `deploy.sh`
ran `docker compose up -d --remove-orphans --wait`, which recreates both `api` replicas (ADR-0045) at the same time
and then `web`. During the v1.1.2 pipeline deploy a 1-second readiness probe from outside the host
(`https://pbip.duckdns.org/api/health/ready`, 2 s timeout) saw a 502 and then no ready answer for about 12-15 s
(#505). Two replicas gave capacity, not continuity.

nginx already resolves `api` through Docker's DNS every 10 s (`default.conf.template`, the upstream is a variable),
so it can follow containers that come and go. What was missing was an order: new replicas healthy first, old ones
out of nginx's view before they stop answering.

Options considered (#505):
1. **Rolling replacement inside `deploy.sh`** with the existing compose project: scale `api` up beside the old
   replicas, wait for their health checks, retire the old ones one at a time.
2. **Blue/green services** (`api-blue`, `api-green`) and an nginx upstream switch. It doubles the service
   definitions and needs a templated upstream and a state file for the active colour.
3. **Accept about 15 s per release** and charge it to the availability budget.

## Decision
Option 1. When `api` containers are already running, `deploy.sh deploy` and `deploy.sh rollback`:
1. pull the images, then start `db` and `redis` (`up -d --wait`, a no-op when they are unchanged);
2. run `migrate` on its own (`up --no-deps --exit-code-from migrate migrate`) while the old release keeps
   serving. From now on a migration must leave the schema usable by the release before it for the length of a
   deploy (add first, remove in a later release); runbook section 2.1 states this rule;
3. `up -d --no-deps --no-recreate --scale api=<running + API_REPLICAS> api`: the new replicas start with the new
   image beside the untouched old ones;
4. wait up to `DEPLOY_WAIT_TIMEOUT` (180 s) for every new replica's health check. If one turns unhealthy, vanishes
   or times out, the new replicas are removed and the deploy fails with the old release still serving; the
   workflow's rollback then finds the old replicas running and replaces them with the same release;
5. retire each old replica in turn: `docker stop -t 30` (SIGTERM: uvicorn stops accepting and finishes its
   in-flight requests; a refused connection is retried on another replica by nginx, which has not sent the
   request yet), `docker rm`, then `nginx -s reload` in `web`, whose new workers resolve `api` at once instead
   of up to 10 s later;
6. replace `worker`, `beat` and `web` (`up -d --no-deps --remove-orphans --wait`).

With no `api` running (a first deploy, or after a stop), the script keeps the single `up -d --remove-orphans
--wait` it had. `API_REPLICAS` in the host `.env` (default 2) sets how many new replicas start, and anything other
than 1-99 is refused before a container changes.

## Consequences
- The api is never without a healthy replica during a release. A broken release no longer takes the site down
  before the health gate catches it: it never receives traffic.
- **`web` is still recreated.** It publishes ports 80 and 443, which two containers cannot share, so the seconds
  between the old nginx stopping and the new one listening remain. The runbook records the measured size with the
  1-second probe; removing it would need a second front proxy and is not done here.
- For about one health-check period the host runs four api replicas; their memory and CPU during a release are
  recorded with the measurement. `beat` stays a single container: it is replaced, not scaled.
- Celery `worker` and `beat` restart after the API, as before; a scheduled task that was running finishes or is
  retried by its own retry policy.
- No new service, dependency, migration or secret. `nginx -s reload` runs inside `web` as its own unprivileged user.
- Tests: `apps/api/tests/unit/test_deploy_script.py` asserts the order with a stub `docker`, the removal of
  unhealthy new replicas without touching the old ones, the refusal of a bad `API_REPLICAS`, and rollback taking
  the same path.
