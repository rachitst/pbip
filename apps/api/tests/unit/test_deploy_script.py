"""Dry run of infra/deploy/deploy.sh against a fake `docker` (TASK-082 definition of done).

No real Docker or host is involved: a stub `docker` on PATH records every invocation, so the tests prove
the command sequence (pull -> health-gated up -> ps), the release bookkeeping, the refusal of non-release
tags, and that a failed start leaves the previous release recorded and recoverable by `rollback`.
"""

import os
import secrets
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path, PureWindowsPath

import pytest
from cryptography import x509

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "infra" / "deploy" / "deploy.sh"
PREFIX = "ghcr.io/example/pbip"


def find_bash(
    *,
    windows: bool,
    which: Callable[[str], str | None],
    is_file: Callable[[str], bool],
    env: Mapping[str, str],
) -> str | None:
    """The bash that can run deploy.sh here, or None.

    #371: on Windows `which("bash")` finds System32's WSL launcher first, and WSL cannot open the Windows
    temp path (exit 127). `Git/bin/bash.exe` is no good either: it is a launcher that prepends
    /mingw64/bin, whose real openssl then shadows the test's fake one. So on Windows only Git for
    Windows' own `<Git>/usr/bin/bash.exe` qualifies, found from git on PATH (`Git/cmd/git.exe`,
    `Git/bin/git.exe` or `Git/mingw64/bin/git.exe`) or the default install folders. There is no
    fallback to `which("bash")` there: no Git Bash means the tests skip rather than run under WSL.
    """
    if not windows:
        return which("bash")
    candidates: list[PureWindowsPath] = []
    git = which("git")
    if git is not None:
        candidates += [
            folder / "usr" / "bin" / "bash.exe" for folder in PureWindowsPath(git).parents
        ]
    for variable, below in (
        ("ProgramW6432", ("Git",)),
        ("ProgramFiles", ("Git",)),
        ("ProgramFiles(x86)", ("Git",)),
        ("LOCALAPPDATA", ("Programs", "Git")),
    ):
        if env.get(variable):
            candidates.append(PureWindowsPath(env[variable], *below, "usr", "bin", "bash.exe"))
    return next((str(path) for path in candidates if is_file(str(path))), None)


BASH = find_bash(
    windows=os.name == "nt", which=shutil.which, is_file=os.path.isfile, env=os.environ
)
needs_bash = pytest.mark.skipif(
    BASH is None, reason="needs bash (Linux/macOS) or Git for Windows' usr/bin/bash.exe (never WSL)"
)

_FAKE_DOCKER = """#!/usr/bin/env bash
# Records "<args> tag=$IMAGE_TAG" and fails `up` when FAKE_DOCKER_FAIL_UP=1.
echo "$* tag=${IMAGE_TAG:-} prefix=${IMAGE_PREFIX:-}" >> "$FAKE_DOCKER_LOG"
# `compose exec -T` gets its input on stdin (the monitoring role SQL): keep it for the assertions.
if [[ "$*" == *" exec -T "* ]]; then
  cat >> "$FAKE_DOCKER_LOG.stdin"
fi
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
    # newline="\n": on Windows write_text would emit CRLF, which bash rejects (`then\r`; #329).
    fake.write_text(_FAKE_DOCKER, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    return tmp_path


def _run(
    host: Path, *args: str, fail_up: bool = False, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    # The fakes in host/bin come first, then the shell's own tools (on Windows `<Git>/usr/bin`, which
    # holds the coreutils deploy.sh calls and is not on a plain PowerShell PATH), then the caller's PATH.
    path = os.pathsep.join([str(host / "bin"), os.path.dirname(BASH), os.environ.get("PATH", "")])
    env = {
        **os.environ,
        "PATH": path,
        "FAKE_DOCKER_LOG": str(host / "docker.log"),
        "FAKE_DOCKER_FAIL_UP": "1" if fail_up else "0",
        "DEPLOY_WAIT_TIMEOUT": "7",
        **(extra_env or {}),
    }
    return subprocess.run(  # noqa: S603 - runs our own script with test-controlled arguments
        [BASH, str(host / "deploy.sh"), *args], env=env, capture_output=True, text=True, check=False
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
    # POSIX permission bits exist on the Linux deploy host, not on NTFS, where Python reports every
    # directory as 0o777 whatever chmod did under Git Bash (#329).
    if os.name == "posix":
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


# --------------------------------------------------------------------------- monitoring (ADR-0059)

# Generated per run, the way the runbook generates them on the host.
_GRAFANA_PW = secrets.token_urlsafe(18)
_MONITOR_PW = secrets.token_urlsafe(18)
_HOST_ENV = (
    "POSTGRES_USER=pbip\nPOSTGRES_DB=pbip\nPUBLIC_HOSTNAME=staging.example.test\n"
    f"GRAFANA_ADMIN_PASSWORD={_GRAFANA_PW}\nPOSTGRES_MONITOR_PASSWORD={_MONITOR_PW}\n"
)


@needs_bash
def test_monitoring_creates_the_read_only_role_then_starts_its_own_project(host: Path) -> None:
    (host / ".env").write_text(_HOST_ENV, encoding="utf-8")

    result = _run(host, "monitoring")

    assert result.returncode == 0, result.stderr
    assert _log(host) == [
        "compose -f docker-compose.prod.yml exec -T db psql -v ON_ERROR_STOP=1 -q -U pbip -d pbip "
        "tag= prefix=",
        "compose -f docker-compose.prod.monitoring.yml pull --quiet tag= prefix=",
        "compose -f docker-compose.prod.monitoring.yml up -d --remove-orphans --wait --wait-timeout 7 "
        "tag= prefix=",
        "compose -f docker-compose.prod.monitoring.yml ps tag= prefix=",
    ]
    sql = (host / "docker.log.stdin").read_text(encoding="utf-8")
    assert "WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pbip_monitor')\\gexec" in sql
    assert "ALTER ROLE pbip_monitor WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE" in sql
    assert f"PASSWORD '{_MONITOR_PW}'" in sql and "GRANT pg_monitor TO pbip_monitor;" in sql
    # Secrets travel on stdin only: never in a command line (ps, shell history) or the script's output.
    for secret in (_GRAFANA_PW, _MONITOR_PW):
        assert secret not in "\n".join(_log(host)) and secret not in result.stdout + result.stderr


@needs_bash
def test_monitoring_writes_the_compose_secret_files_with_restricted_access(host: Path) -> None:
    """The passwords reach the containers as files, never as environment variables: secrets/ is 0700 (only
    the deploy user on the host); the files are 0644 for the containers' own users behind the bind mount."""
    (host / ".env").write_text(_HOST_ENV, encoding="utf-8")

    assert _run(host, "monitoring").returncode == 0

    files = {
        "grafana_admin_password": _GRAFANA_PW,
        "postgres_monitor_password": _MONITOR_PW,
    }
    for name, secret in files.items():
        assert (host / "secrets" / name).read_bytes() == secret.encode()  # no trailing newline
    if os.name == "posix":  # NTFS has no POSIX permission bits (#329)
        assert stat.S_IMODE((host / "secrets").stat().st_mode) == 0o700
        for name in files:
            assert stat.S_IMODE((host / "secrets" / name).stat().st_mode) == 0o644


@needs_bash
def test_a_changed_exporter_password_restarts_the_exporter_only_then(host: Path) -> None:
    (host / ".env").write_text(_HOST_ENV, encoding="utf-8")
    assert _run(host, "monitoring").returncode == 0
    assert _run(host, "monitoring").returncode == 0
    assert not any("restart" in line for line in _log(host))  # first start and unchanged value

    rotated = secrets.token_urlsafe(18)
    (host / ".env").write_text(_HOST_ENV.replace(_MONITOR_PW, rotated), encoding="utf-8")
    assert _run(host, "monitoring").returncode == 0

    assert (host / "secrets" / "postgres_monitor_password").read_text() == rotated
    log = _log(host)
    restart = "compose -f docker-compose.prod.monitoring.yml restart postgres-exporter tag= prefix="
    assert log.count(restart) == 1 and log.index(restart) == len(log) - 2  # after up, before ps


@needs_bash
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GRAFANA_ADMIN_PASSWORD", ""),
        ("GRAFANA_ADMIN_PASSWORD", "admin"),
        ("GRAFANA_ADMIN_PASSWORD", "admin_local_password"),  # the local .env.example value
        (
            "GRAFANA_ADMIN_PASSWORD",
            '<python3 -c "import secrets; print(secrets.token_urlsafe(18))">',
        ),
        ("POSTGRES_MONITOR_PASSWORD", "short-but-random"),
        ("POSTGRES_MONITOR_PASSWORD", "x' ; DROP ROLE pbip; --aaaaaaaaaa"),
    ],
)
def test_monitoring_refuses_default_or_placeholder_secrets_without_touching_docker(
    host: Path, name: str, value: str
) -> None:
    env = "".join(
        f"{name}={value}\n" if line.startswith(f"{name}=") else f"{line}\n"
        for line in _HOST_ENV.splitlines()
    )
    (host / ".env").write_text(env, encoding="utf-8")

    result = _run(host, "monitoring")

    assert result.returncode == 4
    assert f"{name} in .env must be a generated secret" in result.stderr
    assert _log(host) == []
    assert not (host / "secrets").exists()


@needs_bash
def test_monitoring_refuses_to_reuse_the_application_owner_or_an_odd_role_name(host: Path) -> None:
    for role in ("pbip", "Monitor", "x;drop"):
        (host / ".env").write_text(_HOST_ENV + f"POSTGRES_MONITOR_USER={role}\n", encoding="utf-8")
        result = _run(host, "monitoring")
        assert result.returncode == 4, role
        assert "POSTGRES_MONITOR_USER must be" in result.stderr
    assert _log(host) == []


@needs_bash
def test_monitoring_without_a_host_env_refuses(host: Path) -> None:
    result = _run(host, "monitoring")
    assert result.returncode == 4 and "no .env" in result.stderr
    assert _log(host) == []


# --------------------------------------------------------------------------- trained models (ADR-0061)

OVERLAY = "docker-compose.prod.models.yml"
_WITH_MODELS = f"compose -f docker-compose.prod.yml -f {OVERLAY}"


def _with_models(
    host: Path, value: str = "enabled", *, fraud: bool = True, ranker: bool = True
) -> None:
    """The host as section 14.1 of the staging guide leaves it: overlay, model files and the .env flag."""
    shutil.copy(ROOT / "infra" / "deploy" / OVERLAY, host)
    (host / ".env").write_text(
        f"PUBLIC_HOSTNAME=staging.example.test\nML_MODELS={value}\n", encoding="utf-8"
    )
    if fraud:
        (host / "models" / "fraud").mkdir(parents=True)
        (host / "models" / "fraud" / "manifest.json").write_text("{}", encoding="utf-8")
    if ranker:
        (host / "models" / "recommendations").mkdir(parents=True)


@needs_bash
def test_ml_models_enabled_adds_the_overlay_to_every_release_command(host: Path) -> None:
    _with_models(host)

    result = _run(host, "deploy", PREFIX, "v0.1.0")

    assert result.returncode == 0, result.stderr
    assert _log(host) == [
        f"{_WITH_MODELS} pull --quiet tag=v0.1.0 prefix={PREFIX}",
        f"{_WITH_MODELS} up -d --remove-orphans --wait --wait-timeout 7 tag=v0.1.0 prefix={PREFIX}",
        f"{_WITH_MODELS} ps tag=v0.1.0 prefix={PREFIX}",
    ]
    assert "Trained models enabled" in result.stdout


@needs_bash
def test_rollback_uses_the_same_file_set_as_the_deploy(host: Path) -> None:
    _with_models(host)
    assert _run(host, "deploy", PREFIX, "v0.1.0").returncode == 0
    assert _run(host, "deploy", PREFIX, "v0.2.0").returncode == 0

    result = _run(host, "rollback")

    assert result.returncode == 0, result.stderr
    assert _log(host)[-2] == (
        f"{_WITH_MODELS} up -d --remove-orphans --wait --wait-timeout 7 tag=v0.1.0 prefix={PREFIX}"
    )


@needs_bash
@pytest.mark.parametrize("value", ["", "disabled"])
def test_without_ml_models_enabled_the_release_runs_the_rules_only_file(
    host: Path, value: str
) -> None:
    """The default deployment is unchanged, even with the overlay and model files on the host."""
    _with_models(host, value)

    result = _run(host, "deploy", PREFIX, "v0.1.0")

    assert result.returncode == 0, result.stderr
    assert len(_log(host)) == 3
    assert all(line.startswith("compose -f docker-compose.prod.yml ") for line in _log(host))
    assert not any(OVERLAY in line for line in _log(host))


@needs_bash
@pytest.mark.parametrize(
    ("fraud", "ranker", "missing"),
    [
        (False, True, "models/fraud/manifest.json"),
        (True, False, "models/recommendations/"),
        (False, False, "models/fraud/manifest.json models/recommendations/"),
    ],
)
def test_ml_models_enabled_refuses_to_deploy_without_the_model_files(
    host: Path, fraud: bool, ranker: bool, missing: str
) -> None:
    """Docker would create a missing directory empty and the API would quietly serve the rules: the
    deploy stops before any container or state file changes."""
    _with_models(host, fraud=fraud, ranker=ranker)

    result = _run(host, "deploy", PREFIX, "v0.1.0")

    assert result.returncode == 5
    assert f"missing {missing} next to deploy.sh" in result.stderr
    assert _log(host) == []
    assert not (host / ".deployed_tag.pending").exists()
    assert not (host / ".deployed_tag").exists()


@needs_bash
def test_ml_models_enabled_refuses_without_the_overlay_file(host: Path) -> None:
    _with_models(host)
    (host / OVERLAY).unlink()

    result = _run(host, "deploy", PREFIX, "v0.1.0")

    assert result.returncode == 5
    assert f"missing {OVERLAY} next to deploy.sh" in result.stderr
    assert _log(host) == []


@needs_bash
def test_rollback_with_ml_models_enabled_also_refuses_without_the_model_files(host: Path) -> None:
    _with_models(host)
    assert _run(host, "deploy", PREFIX, "v0.1.0").returncode == 0
    assert _run(host, "deploy", PREFIX, "v0.2.0").returncode == 0
    shutil.rmtree(host / "models" / "fraud")
    calls = len(_log(host))

    result = _run(host, "rollback")

    assert result.returncode == 5
    assert "models/fraud/manifest.json" in result.stderr
    assert len(_log(host)) == calls  # docker not touched
    assert (host / ".deployed_tag").read_text() == f"{PREFIX} v0.2.0\n"  # nothing rolled back


@needs_bash
@pytest.mark.parametrize("value", ["enable", "true", "Enabled", "yes"])
def test_an_unknown_ml_models_value_is_refused_rather_than_read_as_off(
    host: Path, value: str
) -> None:
    _with_models(host, value)

    result = _run(host, "deploy", PREFIX, "v0.1.0")

    assert result.returncode == 5
    assert "ML_MODELS in .env must be enabled, disabled or unset" in result.stderr
    assert _log(host) == []


@needs_bash
def test_monitoring_ignores_ml_models(host: Path) -> None:
    """The monitoring path neither needs the model files nor changes its compose files."""
    (host / ".env").write_text(_HOST_ENV + "ML_MODELS=enabled\n", encoding="utf-8")

    result = _run(host, "monitoring")

    assert result.returncode == 0, result.stderr
    assert not any(OVERLAY in line for line in _log(host))


_FAKE_OPENSSL = """#!/usr/bin/env bash
# Records how it was called, then writes the files named by -keyout and -out.
echo "MSYS_NO_PATHCONV=${MSYS_NO_PATHCONV:-unset} $*" >> "$FAKE_OPENSSL_LOG"
while [ $# -gt 0 ]; do
  case "$1" in -keyout | -out) echo fake > "$2"; shift ;; esac
  shift
done
"""


@needs_bash
def test_certificate_subjects_are_shielded_from_git_bash_path_conversion(host: Path) -> None:
    """#329: Git Bash on Windows turns the argument "/CN=db" into "C:/Program Files/Git/CN=db"
    before a native openssl runs, so the deploy stopped after "Creating the PostgreSQL TLS
    certificate". Each openssl call must run with MSYS_NO_PATHCONV=1 and the subject unchanged."""
    fake = host / "bin" / "openssl"
    fake.write_text(_FAKE_OPENSSL, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    (host / ".env").write_text("PUBLIC_HOSTNAME=staging.example.test\n", encoding="utf-8")
    log = host / "openssl.log"

    result = _run(host, "deploy", PREFIX, "v0.1.0", extra_env={"FAKE_OPENSSL_LOG": str(log)})

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert all(call.startswith("MSYS_NO_PATHCONV=1 req ") for call in calls)
    assert " -subj /CN=db " in calls[0]
    assert " -subj /CN=staging.example.test " in calls[1]


# --------------------------------------------------------------------------- bash selection (#371)

_WSL = r"C:\Windows\System32\bash.exe"
_GIT_BASH = r"C:\Program Files\Git\usr\bin\bash.exe"


def _which(**found: str) -> Callable[[str], str | None]:
    return lambda name: found.get(name)


def _files(*present: str) -> Callable[[str], bool]:
    return lambda path: path in present


def test_off_windows_the_bash_on_path_is_used() -> None:
    bash = find_bash(windows=False, which=_which(bash="/usr/bin/bash"), is_file=_files(), env={})
    assert bash == "/usr/bin/bash"


@pytest.mark.parametrize(
    "git",
    [
        r"C:\Program Files\Git\cmd\git.exe",  # the default installer's PATH entry
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files\Git\mingw64\bin\git.exe",
    ],
)
def test_on_windows_git_bash_is_found_next_to_git_and_wsl_is_ignored(git: str) -> None:
    bash = find_bash(
        windows=True,
        which=_which(bash=_WSL, git=git),
        is_file=_files(_WSL, _GIT_BASH, r"C:\Program Files\Git\bin\bash.exe"),
        env={},
    )
    assert bash == _GIT_BASH


def test_on_windows_git_bash_is_found_in_program_files_when_git_is_not_on_path() -> None:
    bash = find_bash(
        windows=True,
        which=_which(bash=_WSL),
        is_file=_files(_WSL, _GIT_BASH),
        env={"ProgramFiles": r"C:\Program Files"},
    )
    assert bash == _GIT_BASH


def test_on_windows_a_per_user_git_install_is_found() -> None:
    user_bash = r"C:\Users\dev\AppData\Local\Programs\Git\usr\bin\bash.exe"
    bash = find_bash(
        windows=True,
        which=_which(bash=_WSL),
        is_file=_files(_WSL, user_bash),
        env={"ProgramFiles": r"C:\Program Files", "LOCALAPPDATA": r"C:\Users\dev\AppData\Local"},
    )
    assert bash == user_bash


def test_on_windows_without_git_bash_the_tests_skip_instead_of_using_wsl() -> None:
    bash = find_bash(
        windows=True,
        which=_which(bash=_WSL, git=r"D:\tools\git\cmd\git.exe"),
        is_file=_files(_WSL),
        env={"ProgramFiles": r"C:\Program Files", "LOCALAPPDATA": r"C:\Users\dev\AppData\Local"},
    )
    assert bash is None
