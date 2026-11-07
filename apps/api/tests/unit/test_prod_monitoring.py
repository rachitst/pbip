"""ADR-0059: the monitoring stack for the production compose project on the staging host.

The overlay cannot be started in CI (it joins the production network of a real host), so this pins the
properties that make it safe and correct: nothing is published beyond loopback, there are no default
credentials, every image is pinned by digest to the local profile's version, every scrape target is a
service the scraper can reach, resources are bounded, and the deploy job ships the configuration it mounts.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
DEPLOY = ROOT / "infra" / "deploy"
OVERLAY = DEPLOY / "docker-compose.prod.monitoring.yml"
PROD = DEPLOY / "docker-compose.prod.yml"
LOCAL = ROOT / "docker-compose.yml"
PROMETHEUS = ROOT / "infra" / "prometheus" / "prometheus.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
# name:version@sha256:<64 hex>, the digest of the multi-arch manifest list (arm64 checked when pinned).
_PINNED = re.compile(r"^(?P<repo>[^:@]+):(?P<tag>[^:@]+)@sha256:[0-9a-f]{64}$")
_MEMORY = re.compile(r"^(?P<n>\d+)(?P<unit>[mg])$")
SOCKET = "/var/run/docker.sock"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _overlay() -> dict[str, Any]:
    return _load(OVERLAY)


def _services(path: Path) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = _load(path)["services"]
    return services


def _networks_of(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks", ["default"])
    return set(networks if isinstance(networks, list) else networks.keys())


def _env_name(value: str) -> str | None:
    match = re.fullmatch(r"\$\{(\w+)(?::?[-?].*)?\}", value)
    return match.group(1) if match else None


def test_is_its_own_project_on_the_production_network() -> None:
    """A release runs `up --remove-orphans` on pbip-prod: in that project these services would be orphans
    and removed by every deploy, so the stack is a separate project that joins pbip-prod's network."""
    overlay, prod = _overlay(), _load(PROD)
    assert overlay["name"] == "pbip-prod-monitoring" != prod["name"]
    assert overlay["networks"]["prod"] == {"external": True, "name": f"{prod['name']}_default"}
    assert not set(_services(OVERLAY)) & set(_services(PROD)), "a service name shadows production"


def test_only_grafana_and_prometheus_publish_ports_and_only_on_loopback() -> None:
    published = {name: s["ports"] for name, s in _services(OVERLAY).items() if "ports" in s}
    assert published == {
        "prometheus": ["127.0.0.1:9090:9090"],
        "grafana": ["127.0.0.1:3000:3000"],
    }
    for name, service in _services(OVERLAY).items():
        assert "expose" not in service and service.get("network_mode") != "host", name


def test_images_are_pinned_by_digest_to_the_local_profile_versions() -> None:
    local = _services(LOCAL)
    for name, service in _services(OVERLAY).items():
        match = _PINNED.match(service["image"])
        assert match, f"{name}: {service['image']} is not pinned as name:version@sha256:digest"
        if name in local:
            # Same release as the local `monitoring` profile, so the dashboards and rules see the same
            # metrics. celery-exporter comes from the project's GHCR build (Docker Hub's is amd64-only).
            assert match["tag"] == local[name]["image"].split("@")[0].rsplit(":", 1)[1], name
            assert "monitoring" in local[name]["profiles"], name


# The passwords and the variables that hold them on the host; none may be interpolated into the file.
_SECRETS = {
    "grafana_admin_password": (
        "grafana",
        "GF_SECURITY_ADMIN_PASSWORD__FILE",
        "GRAFANA_ADMIN_PASSWORD",
    ),
    "postgres_monitor_password": (
        "postgres-exporter",
        "DATA_SOURCE_PASS_FILE",
        "POSTGRES_MONITOR_PASSWORD",
    ),
}


def test_no_default_credentials() -> None:
    services = _services(OVERLAY)
    grafana = services["grafana"]["environment"]
    assert "GF_SECURITY_ADMIN_USER" not in grafana or grafana["GF_SECURITY_ADMIN_USER"] != "admin"
    for flag in (
        "GF_USERS_ALLOW_SIGN_UP",
        "GF_AUTH_ANONYMOUS_ENABLED",
        "GF_SNAPSHOTS_EXTERNAL_ENABLED",
    ):
        assert grafana[flag] == "false", flag
    exporter = services["postgres-exporter"]["environment"]
    assert _env_name(exporter["DATA_SOURCE_USER"]) == "POSTGRES_MONITOR_USER"
    assert "sslmode=verify-full" in exporter["DATA_SOURCE_URI"]
    # Neither password has a fallback anywhere: each comes only from a file deploy.sh writes from .env
    # after refusing a missing or weak value, and compose refuses to start without the file.
    secrets = _overlay()["secrets"]
    assert secrets == {name: {"file": f"./secrets/{name}"} for name in _SECRETS}


def test_passwords_are_never_in_the_compose_file_or_a_container_environment() -> None:
    """``docker inspect`` shows a container's environment: it carries only the path of a mounted secret."""
    services = _services(OVERLAY)
    text = OVERLAY.read_text(encoding="utf-8")
    for name, (service, variable, host_variable) in _SECRETS.items():
        assert services[service]["environment"][variable] == f"/run/secrets/{name}"
        assert services[service]["secrets"] == [name]
        assert host_variable not in text, f"{host_variable} is interpolated into the compose file"
    assert "POSTGRES_PASSWORD" not in text, "the exporter must not hold the owner's password"
    for name, service in services.items():
        for key, value in (service.get("environment") or {}).items():
            if re.search(r"PASS|SECRET|TOKEN|KEY", key):
                assert str(value).startswith("/run/secrets/"), f"{name}.{key} is not a secret file"


def test_nothing_is_sent_outside_the_host_by_grafana() -> None:
    grafana = _services(OVERLAY)["grafana"]["environment"]
    for flag in (
        "GF_ANALYTICS_REPORTING_ENABLED",
        "GF_ANALYTICS_CHECK_FOR_UPDATES",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES",
        "GF_NEWS_NEWS_FEED_ENABLED",
    ):
        assert grafana[flag] == "false", flag
    assert grafana["GF_PLUGINS_PREINSTALL_DISABLED"] == "true"


def _target_hosts(job: dict[str, Any]) -> list[tuple[str, str]]:
    """(scraper, host) pairs: Prometheus scrapes the address; a blackbox job's targets are probed by
    the exporter the relabelling points __address__ at."""
    targets = [t for sc in job.get("static_configs", []) for t in sc["targets"]]
    targets += [n for sd in job.get("dns_sd_configs", []) for n in sd["names"]]
    prober = next(
        (
            r["replacement"]
            for r in job.get("relabel_configs", [])
            if r.get("target_label") == "__address__"
        ),
        None,
    )
    pairs = []
    for target in targets:
        host = urlsplit(target).hostname if "://" in target else target.split(":")[0]
        assert host, target
        pairs.append(("prometheus" if prober is None else prober.split(":")[0], host))
        if prober is not None:
            pairs.append(("prometheus", prober.split(":")[0]))
    return pairs


def test_every_scrape_target_is_a_service_its_scraper_can_reach() -> None:
    """The shared prometheus.yml is the production config: every host it names is a production service
    (api, web) or a monitoring service, on a network the scraping or probing container is attached to."""
    overlay, prod = _services(OVERLAY), _services(PROD)
    networks = {name: {"prod"} for name in prod} | {n: _networks_of(s) for n, s in overlay.items()}
    pairs = [p for job in _load(PROMETHEUS)["scrape_configs"] for p in _target_hosts(job)]
    assert {host for _, host in pairs} >= {"api", "web", "redis-exporter", "postgres-exporter"}
    for scraper, host in pairs:
        if host == "localhost":
            assert scraper == "prometheus"  # Prometheus scraping itself
            continue
        assert host in networks, (
            f"{host} is not a service of docker-compose.prod.yml or the overlay"
        )
        assert networks[scraper] & networks[host], f"{scraper} cannot reach {host}"


def test_exporters_point_at_production_services() -> None:
    services, prod = _services(OVERLAY), _services(PROD)
    hosts = {
        "celery-exporter": urlsplit(
            services["celery-exporter"]["command"][0].split("=", 1)[1]
        ).hostname,
        "redis-exporter": urlsplit(
            services["redis-exporter"]["command"][0].split("=", 1)[1]
        ).hostname,
        "postgres-exporter": services["postgres-exporter"]["environment"]["DATA_SOURCE_URI"].split(
            ":"
        )[0],
    }
    assert hosts == {
        "celery-exporter": "redis",
        "redis-exporter": "redis",
        "postgres-exporter": "db",
    }
    for name, host in hosts.items():
        assert host in prod and "prod" in _networks_of(services[name]), name


def test_production_worker_emits_task_events_for_the_celery_exporter() -> None:
    assert "-E" in _services(PROD)["worker"]["command"]


def _memory_mib(value: str) -> int:
    match = _MEMORY.match(value)
    assert match, value
    return int(match["n"]) * (1024 if match["unit"] == "g" else 1)


def test_every_service_has_bounded_memory_cpu_and_retention() -> None:
    """2 OCPU / 12 GB host shared with the production stack: the whole stack is capped at 3 GiB."""
    services = _services(OVERLAY)
    total = 0
    for name, service in services.items():
        limits = service["deploy"]["resources"]["limits"]
        assert 0 < float(limits["cpus"]) <= 0.5, name
        total += _memory_mib(limits["memory"])
        assert service["restart"] == "unless-stopped", name
    assert total <= 3 * 1024
    prometheus = services["prometheus"]["command"]
    assert "--storage.tsdb.retention.time=35d" in prometheus  # the monthly availability figure
    assert any(arg.startswith("--storage.tsdb.retention.size=") for arg in prometheus)
    loki = services["loki"]["command"]
    assert "-compactor.retention-enabled=true" in loki
    assert any(arg.startswith("-store.retention=") for arg in loki)


def test_only_the_api_proxy_touches_the_docker_socket_and_it_is_read_only() -> None:
    """Docker socket access is root on the host: Alloy reads containers through a filtering proxy that
    allows GETs only, on an internal network shared with Alloy alone."""
    services, networks = _services(OVERLAY), _overlay()["networks"]
    for name, service in services.items():
        mounts = [str(v) for v in service.get("volumes", [])]
        if name == "docker-socket-proxy":
            assert mounts == [f"{SOCKET}:{SOCKET}:ro"]
            continue
        assert not any(SOCKET in m for m in mounts), name
        assert service["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in service["security_opt"], name
    proxy = services["docker-socket-proxy"]
    assert proxy["environment"]["POST"] == "0"
    assert _networks_of(proxy) == {"docker-api"} and networks["docker-api"]["internal"] is True
    assert [n for n, s in services.items() if "docker-api" in _networks_of(s)] == [
        "alloy",
        "docker-socket-proxy",
    ]
    assert services["alloy"]["environment"]["DOCKER_HOST"] == "tcp://docker-socket-proxy:2375"
    assert services["alloy"]["user"] != "0"
    alloy = (ROOT / "infra" / "alloy" / "config.alloy").read_text(encoding="utf-8")
    assert alloy.count(f'coalesce(sys.env("DOCKER_HOST"), "unix://{SOCKET}")') == 2


def _monitoring_sources() -> set[str]:
    sources = set()
    for service in _services(OVERLAY).values():
        for volume in service.get("volumes", []):
            source = str(volume).split(":")[0]
            if source.startswith("./monitoring/"):
                sources.add(source.removeprefix("./monitoring/"))
    return sources


def test_mounted_configuration_is_the_repository_s_and_the_deploy_job_ships_it() -> None:
    """~/pbip/monitoring is a copy of infra/{prometheus,grafana,loki,alloy}: no forked dashboards."""
    sources = _monitoring_sources()
    assert sources >= {"prometheus", "grafana/provisioning", "grafana/dashboards", "loki", "alloy"}
    for source in sources:
        assert (ROOT / "infra" / source).is_dir(), source
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy"]["steps"]
    copy = next(s for s in steps if s.get("name", "").startswith("Copy"))["run"]
    assert "infra/deploy/docker-compose.prod.monitoring.yml" in copy
    tar = next(line for line in copy.splitlines() if line.strip().startswith("tar -C infra"))
    shipped = set(tar.split("-cf -")[1].split("|")[0].split())
    assert {source.split("/")[0] for source in sources} <= shipped
    assert "~/pbip/monitoring" in tar


def _render_script() -> str:
    """The blackbox entrypoint with compose's `$$` escapes resolved, as the container runs it."""
    command: list[str] = _services(OVERLAY)["blackbox-exporter"]["command"]
    return command[0].replace("$$", "$")


needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def _render(tmp_path: Path, hostname: str) -> subprocess.CompletedProcess[str]:
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    shutil.copy(ROOT / "infra" / "prometheus" / "blackbox.yml", src / "blackbox.yml")
    # One pass, so a replacement (tmp_path is itself under /tmp) is never rewritten again.
    paths = {
        "/etc/pbip-blackbox/": f"{src}/",
        "/tmp/": f"{out}/",  # noqa: S108 - the container path being rewritten, not a file we use
        "exec /bin/blackbox_exporter --config.file=": "cat ",
    }
    script = re.sub("|".join(map(re.escape, paths)), lambda m: paths[m.group(0)], _render_script())
    sh = shutil.which("sh")
    assert sh is not None
    return subprocess.run(  # noqa: S603 - our own script with test-controlled input
        [sh, "-ec", script],
        env={"PUBLIC_HOSTNAME": hostname, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@needs_sh
def test_web_probe_verifies_the_certificate_for_the_public_host_name(tmp_path: Path) -> None:
    result = _render(tmp_path, "staging.example.test")
    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(result.stdout)
    shared = _load(ROOT / "infra" / "prometheus" / "blackbox.yml")
    tls = rendered["modules"]["https_2xx"]["http"]["tls_config"]
    assert tls == {"ca_file": "/etc/blackbox/ca.crt", "server_name": "staging.example.test"}
    shared["modules"]["https_2xx"]["http"]["tls_config"]["server_name"] = "staging.example.test"
    assert rendered == shared  # nothing else differs from the local probes
    ca_mount = _services(OVERLAY)["blackbox-exporter"]["volumes"][1]
    assert ca_mount.endswith(":/etc/blackbox/ca.crt:ro") and "/etc/ssl/certs/" in ca_mount


@needs_sh
@pytest.mark.parametrize("hostname", ["", "evil/x", "a b", "x;rm -rf /", "host\\name"])
def test_web_probe_refuses_a_host_name_that_is_not_one(tmp_path: Path, hostname: str) -> None:
    result = _render(tmp_path, hostname)
    assert result.returncode != 0
    assert "server_name" not in result.stdout
