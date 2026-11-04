# Deployment diagrams

Two deployments exist: the local Docker Compose stack every developer runs (`docker-compose.yml`, `make up`), and the
staging target, one Oracle Cloud Always Free Arm VM running `infra/deploy/docker-compose.prod.yml`
([Oracle guide](../operations/deploy-oracle-cloud-arm.md),
[runbook](../operations/runbook.md)).

| Diagram | Source |
|---|---|
| [1. Docker Compose stack](#1-docker-compose-stack) | `docker-compose.yml`, `scripts/dev.py` (`up`, `up-all`) |
| [2. Oracle Cloud Arm staging](#2-oracle-cloud-arm-staging) | `infra/deploy/docker-compose.prod.yml`, `infra/deploy/deploy.sh`, `.github/workflows/deploy.yml`, `docs/operations/deploy-oracle-cloud-arm.md` |

## 1. Docker Compose stack

Exports: [SVG](svg/deployment-compose.svg), [PNG](png/deployment-compose.png).
Project name `pbip`. Only nginx listens on all interfaces; every other published port is bound to `127.0.0.1`.
`make up` starts the default services; `make up-all` adds the `monitoring` profile and MLflow; the `otel` profile is
started on its own.

<!-- diagram: deployment-compose -->
```mermaid
flowchart TB
    dev["Developer laptop<br/>browser, make, uv, Node 22"]
    ollama["Ollama on the host :11434<br/>reached as host.docker.internal"]

    subgraph docker["Docker Engine, compose project pbip"]
        subgraph core_services["default services"]
            web["web<br/>0.0.0.0:8080 redirect, 0.0.0.0:8443 HTTPS<br/>certs from infra/certs/web"]
            api["api x API_REPLICAS<br/>127.0.0.1:8000-8003<br/>mounts models/fraud, models/recommendations read-only"]
            migrate["migrate<br/>alembic upgrade head, then exits"]
            worker["worker<br/>celery worker, concurrency 2, task events"]
            beat["beat<br/>celery beat"]
            db[("db: postgres:16-alpine<br/>127.0.0.1:5432, volume pgdata, TLS")]
            redis[("redis: redis:7-alpine<br/>127.0.0.1:6379, volume redisdata")]
        end
        subgraph monitoring["profile monitoring"]
            prom["prometheus v3.5.0<br/>127.0.0.1:9090"]
            graf["grafana 12.1.0<br/>127.0.0.1:3000"]
            loki["loki 3.5.0"]
            alloy["alloy v1.10.0<br/>reads the Docker socket"]
            exp["blackbox, celery, redis and<br/>postgres exporters"]
        end
        subgraph mlp["profile ml"]
            mlflow["mlflow v3.5.0<br/>127.0.0.1:5000"]
        end
        subgraph otelp["profile otel"]
            otel["otel-collector 0.164.0<br/>127.0.0.1:4318"]
        end
    end

    dev -- "https://localhost:8443" --> web
    web --> api
    migrate --> db
    api --> db
    api --> redis
    api --> ollama
    worker --> db
    worker --> redis
    beat --> redis
    prom --> api
    prom --> exp
    alloy --> loki
    graf --> prom
    graf --> loki
    api -.-> otel
    dev -. "make fraud-model, make recommendation-model" .-> mlflow
```

## 2. Oracle Cloud Arm staging

Exports: [SVG](svg/deployment-oracle-arm.svg), [PNG](png/deployment-oracle-arm.png).
One `VM.Standard.A1.Flex` instance (Ampere, arm64) with Ubuntu 24.04. Images are built for `linux/amd64` and
`linux/arm64` and pulled from GHCR. A release tag `vX.Y.Z` on `main` triggers the Deploy workflow; the SSH deploy job
runs only when the repository variable `DEPLOY_ENABLED` is `true`, otherwise the owner deploys by hand with the same
`deploy.sh`. Staging has no monitoring profile and no Ollama.

<!-- diagram: deployment-oracle-arm -->
```mermaid
flowchart TB
    subgraph github["GitHub"]
        tag["Tag vX.Y.Z pushed from main"]
        verify["Deploy: verify<br/>tag format, commit on main"]
        package["Deploy: package<br/>build api and web for amd64 and arm64"]
        ghcr[("GHCR<br/>ghcr.io/owner/pbip/api, /web")]
        deployjob["Deploy: deploy<br/>only when DEPLOY_ENABLED=true<br/>staging environment secrets"]
    end
    owner["Owner laptop<br/>manual deploy over SSH"]
    users["Testers' browsers"]

    subgraph oci["Oracle Cloud tenancy, home region"]
        subgraph vcn["VCN, public subnet, security list:<br/>443 open, 80 open with Let's Encrypt, 22 restricted"]
            subgraph vm["VM.Standard.A1.Flex, Ubuntu 24.04 aarch64, host firewall"]
                subgraph prod["compose project pbip-prod in ~/pbip"]
                    pweb["web<br/>:80 and :443 published<br/>TLS from certs/web, ACME webroot"]
                    papi["api x 2 replicas"]
                    pmig["migrate"]
                    pworker["worker"]
                    pbeat["beat"]
                    pdb[("db PostgreSQL 16<br/>no published port")]
                    predis[("redis 7<br/>no published port")]
                end
                files[/"~/pbip: docker-compose.prod.yml, .env,<br/>deploy.sh, .deployed_tag, .previous_tag,<br/>geoip/, certs/"/]
                certbot["certbot timer<br/>renews, deploy hook reloads nginx"]
                probe["systemd timer every 30 s<br/>uptime-probe.sh to /api/health/ready"]
                backup["scripts/backup.py<br/>gpg-encrypted dump and manifest"]
            end
        end
        bucket[("Object Storage bucket pbip-backups<br/>versioned, write-only instance principal")]
    end

    tag --> verify --> package --> ghcr
    package --> deployjob
    deployjob -- "SSH, known host pinned:<br/>copy files, deploy.sh deploy, smoke test,<br/>deploy.sh rollback on failure" --> files
    owner -- "SSH: deploy.sh deploy / rollback" --> files
    files --> prod
    ghcr -- "docker compose pull" --> prod
    users -- "HTTPS 443" --> pweb
    pweb --> papi
    pmig --> pdb
    papi --> pdb
    papi --> predis
    pworker --> pdb
    pworker --> predis
    pbeat --> predis
    certbot --> pweb
    probe --> pweb
    backup --> pdb
    backup -- "oci os object put" --> bucket
```

**What is not drawn because it does not exist on staging:** the monitoring and OTel profiles, MLflow, Ollama and the
Groq key (staging sets no `OLLAMA_*` variables, so the assistant answers through its rules route), and demo data
(`scripts/seed_dev.py` refuses `APP_ENV=staging`).

**Extension point.** A new staging service is a new node inside `pbip-prod`, with its published port only if
`docker-compose.prod.yml` publishes one.
