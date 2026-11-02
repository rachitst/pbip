"""Dry run of infra/deploy/deploy.sh against a fake `docker` (TASK-082 definition of done).

No real Docker or host is involved: a stub `docker` on PATH records every invocation, so the tests prove
the command sequence (pull -> health-gated up -> ps), the release bookkeeping, the refusal of non-release
tags, and that a failed start leaves the previous release recorded and recoverable by `rollback`.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography import x509

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "infra" / "deploy" / "deploy.sh"
PREFIX = "ghcr.io/example/pbip"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (Linux/macOS)")

_FAKE_DOCKER = """#!/usr/bin/env bash
# Records "<args> tag=$IMAGE_TAG" and fails `up` when FAKE_DOCKER_FAIL_UP=1.
echo "$* tag=${IMAGE_TAG:-} prefix=${IMAGE_PREFIX:-}" >> "$FAKE_DOCKER_LOG"
if [ "${FAKE_DOCKER_FAIL_UP:-0}" = "1" ] && [[ "$*" == *" up "* ]]; then
  echo "simulated health-check timeout" >&2
  exit 1
fi
"""


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A fake ~/pbip: the script, the compose file and a stub docker first on PATH."""
    shutil.copy(SCRIPT, tmp_path / "deploy.sh")
    shutil.copy(ROOT / "infra" / "deploy" / "docker-compose.prod.yml", tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    return tmp_path


def _run(host: Path, *args: str, fail_up: bool = False) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{host / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(host / "docker.log"),
        "FAKE_DOCKER_FAIL_UP": "1" if fail_up else "0",
        "DEPLOY_WAIT_TIMEOUT": "7",
    }
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603 - runs our own script with test-controlled arguments
        [bash, str(host / "deploy.sh"), *args], env=env, capture_output=True, text=True, check=False
    )


def _log(host: Path) -> list[str]:
    log = host / "docker.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@needs_bash
def test_deploy_pulls_then_starts_health_gated_and_records_the_release(host: Path) -> None:
    result = _run(host, "deploy", PREFIX, "v0.1.0")

    assert result.returncode == 0, result.stderr
    assert _log(host) == [
        f"compose -f docker-compose.prod.yml pull --quiet tag=v0.1.0 prefix={PREFIX}",
        f"compose -f docker-compose.prod.yml up -d --remove-orphans --wait --wait-timeout 7 "
        f"tag=v0.1.0 prefix={PREFIX}",
        f"compose -f docker-compose.prod.yml ps tag=v0.1.0 prefix={PREFIX}",
    ]
    assert (host / ".deployed_tag").read_text() == f"{PREFIX} v0.1.0\n"
    assert not (host / ".previous_tag").exists()
    assert not (host / ".deployed_tag.pending").exists()


@needs_bash
def test_second_deploy_keeps_the_previous_release_and_rollback_restores_it(host: Path) -> None:
    assert _run(host, "deploy", PREFIX, "v0.1.0").returncode == 0
    assert _run(host, "deploy", PREFIX, "v0.2.0").returncode == 0
    assert (host / ".deployed_tag").read_text() == f"{PREFIX} v0.2.0\n"
    assert (host / ".previous_tag").read_text() == f"{PREFIX} v0.1.0\n"

    result = _run(host, "rollback")

    assert result.returncode == 0, result.stderr
    assert (host / ".deployed_tag").read_text() == f"{PREFIX} v0.1.0\n"
    assert _log(host)[-2].endswith(f"--wait-timeout 7 tag=v0.1.0 prefix={PREFIX}")
    assert "migrations are not downgraded" in result.stdout


@needs_bash
def test_failed_start_leaves_the_previous_release_recorded_for_rollback(host: Path) -> None:
    assert _run(host, "deploy", PREFIX, "v0.1.0").returncode == 0

    failed = _run(host, "deploy", PREFIX, "v0.2.0", fail_up=True)

    assert failed.returncode != 0
    assert "simulated health-check timeout" in failed.stderr
    assert (host / ".deployed_tag").read_text() == f"{PREFIX} v0.1.0\n"  # not advanced
    assert (host / ".deployed_tag.pending").read_text() == f"{PREFIX} v0.2.0\n"
    assert _run(host, "rollback").returncode == 0
    assert (host / ".deployed_tag").read_text() == f"{PREFIX} v0.1.0\n"
    assert not (host / ".deployed_tag.pending").exists()


@needs_bash
def test_refuses_non_release_tags_without_touching_docker(host: Path) -> None:
    for bad in ("latest", "main", "v1.2", "v1.2.3-rc1"):
        result = _run(host, "deploy", PREFIX, bad)
        assert result.returncode == 2, bad
        assert "only release tags vX.Y.Z" in result.stderr
    assert _log(host) == []
    assert not (host / ".deployed_tag").exists()


@needs_bash
def test_rollback_without_a_previous_release_fails_clearly(host: Path) -> None:
    result = _run(host, "rollback")
    assert result.returncode == 3
    assert "nothing to roll back to" in result.stderr
    assert _log(host) == []


@needs_bash
def test_usage_error_for_unknown_commands(host: Path) -> None:
    assert _run(host, "restart").returncode == 64
    assert _run(host).returncode == 64


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")


@needs_bash
@needs_openssl
def test_deploy_creates_tls_material_once_and_never_overwrites_it(host: Path) -> None:
    """NFR-02: nginx and PostgreSQL refuse to start without certificates (runbook 1.4)."""
    (host / ".env").write_text("PUBLIC_HOSTNAME=staging.example.test\n", encoding="utf-8")
    assert _run(host, "deploy", PREFIX, "v0.1.0").returncode == 0
    certs = host / "certs"
    db_crt, web_crt, web_key = (
        certs / "db" / "server.crt",
        certs / "web" / "tls.crt",
        certs / "web" / "tls.key",
    )
    assert stat.S_IMODE(certs.stat().st_mode) == 0o700
    assert stat.S_IMODE((certs / "db" / "server.key").stat().st_mode) == 0o600
    assert (
        stat.S_IMODE(web_key.stat().st_mode) == 0o644
    )  # nginx-unprivileged reads it via a bind mount
    names = (
        x509.load_pem_x509_certificate(web_crt.read_bytes())
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value.get_values_for_type(x509.DNSName)
    )
    assert names == ["staging.example.test"]
    before = db_crt.read_bytes(), web_crt.read_bytes()

    assert _run(host, "deploy", PREFIX, "v0.2.0").returncode == 0
    assert (
        db_crt.read_bytes(),
        web_crt.read_bytes(),
    ) == before  # certbot's certificate is never replaced


@needs_bash
@needs_openssl
def test_deploy_without_a_host_env_uses_a_localhost_placeholder(host: Path) -> None:
    assert not (host / ".env").exists()
    result = _run(host, "deploy", PREFIX, "v0.1.0")
    assert result.returncode == 0, result.stderr
    assert "placeholder web certificate for localhost" in result.stdout
