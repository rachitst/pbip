# Component diagram

Exports: [SVG](svg/component.svg), [PNG](png/component.png).

Every runtime component of PBIP and how they talk, from `docker-compose.yml`, `infra/nginx/default.conf.template`,
`infra/prometheus/prometheus.yml`, `infra/otel/collector.yml` and `apps/api/src/pbip_api/worker/celery_app.py`. The
[HLD container view](../architecture/HLD.md#3-c4-level-2-containers) is the C4 level 2 version of this picture; this
diagram adds the beat schedule, the ML package boundary and the optional profiles.

<!-- diagram: component -->
```mermaid
flowchart LR
    browser["Browser<br/>React 19 + TypeScript SPA<br/>TanStack Query, typed client from OpenAPI"]

    subgraph edge["web container"]
        nginx["nginx-unprivileged<br/>:8080 redirect, :8443 TLS<br/>static SPA, CSP and security headers<br/>/api/ proxied, /metrics answers 404"]
    end

    subgraph app["api containers (API_REPLICAS)"]
        fastapi["FastAPI modular monolith<br/>one uvicorn process per container<br/>routers, services, policies, ledger"]
        mlserve["pbip_ml serving, in-process<br/>fraud contract, features, hybrid inference<br/>recommendation engine and ranker"]
        fastapi --> mlserve
    end

    subgraph jobs["Celery"]
        beat["beat (single instance)<br/>relay-outbox 10 s<br/>execute-scheduled-transfers 60 s<br/>expire-stale-step-ups 60 s<br/>pay-out-matured-deposits hourly :05<br/>evaluate-feature-drift hourly :20<br/>monitor-structuring-deposits hourly :35<br/>run-fund-sips hourly :50<br/>ensure-clickstream-partitions 01:30<br/>materialize-monthly-spending 02:00<br/>segment-customers-weekly Sunday 03:00<br/>build-behaviour-profiles-daily 03:40<br/>purge-expired-idempotency-keys 04:15<br/>purge-processed-outbox-events 04:30"]
        worker["worker<br/>runs the tasks with the same<br/>services as the API"]
    end

    subgraph data["Data stores"]
        pg[("PostgreSQL 16<br/>TLS verify-full<br/>schemas public and synthetic")]
        redis[("Redis 7<br/>Celery broker, rate limits,<br/>velocity counters, monitoring cache,<br/>worker metric totals")]
    end

    models[/"Model files, read-only mounts<br/>models/fraud, models/recommendations"/]
    geoip[/"DB-IP Lite country and city files<br/>optional"/]

    subgraph llm["LLM providers, assistant routing only"]
        ollama["Ollama on the host<br/>host.docker.internal:11434<br/>default qwen2.5:1.5b-instruct"]
        groq["Groq API, optional<br/>LLM_PROVIDER=groq"]
    end

    subgraph mon["profile: monitoring"]
        prom["Prometheus<br/>scrape and alert rules"]
        graf["Grafana<br/>api, fraud, platform,<br/>availability, overview dashboards"]
        loki["Loki"]
        alloy["Alloy<br/>Docker log collector"]
        exporters["redis-exporter, postgres-exporter,<br/>celery-exporter, blackbox-exporter"]
    end
    otel["OpenTelemetry Collector<br/>profile: otel, OTLP/HTTP :4318"]
    mlflow["MLflow<br/>profile: ml, training runs only"]

    browser -- "HTTPS" --> nginx
    nginx -- "HTTP /api/" --> fastapi
    fastapi -- "SQLAlchemy 2, psycopg 3" --> pg
    fastapi -- "redis-py" --> redis
    fastapi -. "loaded once at start-up" .-> models
    fastapi -. "read per request when configured" .-> geoip
    fastapi -- "unclear questions only, 4 s timeout" --> ollama
    fastapi -- "redacted question, tool schemas" --> groq
    beat -- "publish tasks" --> redis
    worker -- "consume tasks" --> redis
    worker --> pg
    prom -- "scrape /metrics on every replica" --> fastapi
    prom --> exporters
    exporters --> redis
    exporters --> pg
    exporters -- "probe /api/health/ready" --> nginx
    alloy -- "container logs" --> loki
    graf --> prom
    graf --> loki
    fastapi -. "spans when OTEL_ENABLED" .-> otel
    worker -. "spans" .-> otel
    beat -. "spans" .-> otel
```

| Component | Defined in | Notes |
|---|---|---|
| web (nginx) | `apps/web/Dockerfile`, `infra/nginx/` | The only published port in staging (`infra/deploy/docker-compose.prod.yml`) |
| api | `apps/api/Dockerfile` | Replicas scale with `API_REPLICAS` (ADR-0045); `migrate` runs `alembic upgrade head` first |
| worker, beat | same image, `celery ... worker` and `celery ... beat` | Beat must stay a single instance; the schedule is in `worker/celery_app.py` |
| PostgreSQL, Redis | `postgres:16-alpine`, `redis:7-alpine` | Loopback-only ports in the local stack, none in staging |
| pbip_ml | `ml/src/pbip_ml` | Imported in-process; training code never runs on the request path (import contracts in [module.md](module.md#3-import-contracts)) |
| LLM providers | Ollama on the host; Groq optional (ADR-0018, ADR-0037) | Not provisioned in the staging compose; the rules route always answers |
| Monitoring | `docker-compose.yml` profiles `monitoring` and `otel` | Local stack only; staging has container logs and the availability probe |
| MLflow | profile `ml` | Experiment tracking and the model registry; not used on the request path |

**Extension point.** A new container is a new node inside the matching group and a row in the table; a new beat job
is a new line in the beat node, copied from `beat_schedule`.
