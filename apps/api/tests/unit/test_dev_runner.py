"""Unit tests for PBIP developer command runner (scripts/dev.py).

Verifies:
- Command dispatch, argument forwarding, and unknown command handling.
- Help flag handling and command overview output.
- Subprocess execution success and failure exit code behavior.
- Celery worker Windows vs Unix argument handling (--pool=solo on Windows).
- Clean command removes cache artifacts and build outputs.
- Path resolution logic for executables.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pytest

import scripts.dev as dev_mod
from scripts.dev import (
    _needs_resolution,
    _run,
    cmd_clean,
    cmd_datagen,
    cmd_dev_worker,
    cmd_help,
    main,
)

pytestmark = pytest.mark.unit


def test_main_help_dispatch(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify help is dispatched on help, --help, -h, or no arguments."""
    for args in [
        ["dev.py"],
        ["dev.py", "help"],
        ["dev.py", "--help"],
        ["dev.py", "-h"],
    ]:
        with patch("sys.argv", args):
            code = main()
            assert code == 0
            out = capsys.readouterr().out
            assert "PBIP Developer Command Runner" in out
            assert "Available Commands:" in out
            assert "doctor" in out
            assert "setup" in out


def test_main_unknown_command_handling(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify unknown commands return exit code 1 and emit a clear error."""
    with patch("sys.argv", ["dev.py", "unknown-action"]):
        code = main()
        assert code == 1
        err = capsys.readouterr().err
        assert "Unknown command: 'unknown-action'" in err


def test_main_argument_forwarding() -> None:
    """Verify command arguments are properly forwarded to matching handler."""
    mock_handler = MagicMock(returncode=0)
    mock_handler.return_value = 0

    with (
        patch.dict(dev_mod.__dict__, {"cmd_test_sample": mock_handler}),
        patch("sys.argv", ["dev.py", "test-sample", "--arg1", "val1", "--verbose"]),
    ):
        code = main()
        assert code == 0
        mock_handler.assert_called_once_with(["--arg1", "val1", "--verbose"])


def test_cmd_help_lists_all_registered_commands(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify cmd_help prints all commands from COMMANDS registry."""
    code = cmd_help([])
    assert code == 0
    out = capsys.readouterr().out
    for cmd_name in dev_mod.COMMANDS:
        assert cmd_name in out


def test_cmd_dev_worker_unix() -> None:
    """On Linux/macOS worker and beat share one process; no solo pool."""
    with (
        patch("scripts.dev.IS_WINDOWS", False),
        patch("scripts.dev._run", return_value=0) as mock_run,
    ):
        code = cmd_dev_worker(["--concurrency=4"])
        assert code == 0
        mock_run.assert_called_once()
        cmd_args: list[str] = mock_run.call_args[0][0]
        assert "--pool=solo" not in cmd_args
        assert "--beat" in cmd_args
        assert cmd_args[-1] == "--concurrency=4"
        assert "celery" in cmd_args


def test_cmd_dev_worker_windows_runs_beat_separately() -> None:
    """Celery rejects --beat on Windows: beat must be a separate process, worker uses solo."""
    beat = MagicMock()
    with (
        patch("scripts.dev.IS_WINDOWS", True),
        patch("scripts.dev._find_tool", return_value="uv"),
        patch("scripts.dev.subprocess.Popen", return_value=beat) as mock_popen,
        patch("scripts.dev._run", return_value=3) as mock_run,
    ):
        code = cmd_dev_worker(["--concurrency=1"])
    assert code == 3  # the worker's exit code is propagated
    beat_args: list[str] = mock_popen.call_args[0][0]
    assert beat_args[-2:] == ["beat", "--loglevel=INFO"]
    worker_args: list[str] = mock_run.call_args[0][0]
    assert "--beat" not in worker_args and "-B" not in worker_args
    assert "--pool=solo" in worker_args and worker_args[-1] == "--concurrency=1"
    beat.terminate.assert_called_once()  # beat never outlives the worker


def test_cmd_dev_worker_windows_stops_beat_even_if_the_worker_crashes() -> None:
    beat = MagicMock()
    with (
        patch("scripts.dev.IS_WINDOWS", True),
        patch("scripts.dev._find_tool", return_value=None),
        patch("scripts.dev.subprocess.Popen", return_value=beat),
        patch("scripts.dev._run", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        cmd_dev_worker([])
    beat.terminate.assert_called_once()


def test_dev_cmd_propagates_exit_codes() -> None:
    """Regression: %ERRORLEVEL% inside a parenthesised block is expanded when the block is
    parsed (always 0 there), so dev.cmd exited 0 even when the command failed. A bare
    "exit /b" sets ERRORLEVEL but cmd.exe itself still exits 0 under `cmd /c` or PowerShell
    (native Windows 10 run, #12), so the code must be passed explicitly via "call"."""
    text = (dev_mod.ROOT / "dev.cmd").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()]
    depth = 0
    for line in lines:
        if depth > 0 and not line.upper().startswith("REM"):
            assert re.search(r"(?<!%)%ERRORLEVEL%(?!%)", line.upper()) is None, line
        depth += line.count("(") - line.count(")")
    runs = [i for i, line in enumerate(lines) if line.endswith('scripts\\dev.py" %*')]
    assert len(runs) == 2
    for index in runs:
        assert lines[index + 1].lower() == "call exit /b %%errorlevel%%"
    # setlocal EnableDelayedExpansion resets ERRORLEVEL to 0 and strips "!" from arguments.
    code_lines = [line.lower() for line in lines if not line.upper().startswith("REM")]
    assert not any("enabledelayedexpansion" in line for line in code_lines)
    assert not any(line.startswith(":") for line in lines)  # no labels (LF-stored file)


@pytest.mark.skipif(sys.platform != "win32", reason="runs the real cmd.exe")
@pytest.mark.parametrize("code", [0, 1, 3])
def test_dev_cmd_exit_code_reaches_the_calling_process(tmp_path: Path, code: int) -> None:
    """dev.cmd run through cmd.exe must exit with the runner's code (CI and PowerShell read
    the process exit code, not ERRORLEVEL). A stub runner stands in for scripts/dev.py."""
    shutil.copy(dev_mod.ROOT / "dev.cmd", tmp_path / "dev.cmd")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "dev.py").write_text(
        "import sys\nraise SystemExit(int(sys.argv[1]))\n", encoding="utf-8"
    )
    system32 = Path(os.environ["SYSTEMROOT"]) / "System32"
    # Only this interpreter's directory and System32 on PATH: no uv, so dev.cmd uses `python`.
    path = os.pathsep.join([str(Path(sys.executable).parent), str(system32)])
    env = {**os.environ, "PATH": path}
    proc = subprocess.run(  # noqa: S603
        [str(system32 / "cmd.exe"), "/c", str(tmp_path / "dev.cmd"), str(code)],
        env=env,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == code


def test_dev_ps1_is_a_simple_script_that_forwards_args() -> None:
    """Regression: [CmdletBinding()] made -p/-v/-d bind to -PipelineVariable/-Verbose/-Debug
    (the runner never ran and exited 0), and $ErrorActionPreference = "Stop" aborted the run
    on the first stderr line when the caller redirected it under Windows PowerShell 5.1."""
    code = [
        ln.strip().lower()
        for ln in (dev_mod.ROOT / "dev.ps1").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert not any(ln.startswith(("[cmdletbinding", "param(")) for ln in code)
    assert not any(ln.startswith("$erroractionpreference") for ln in code)
    assert sum('scripts\\dev.py" @args' in ln for ln in code) == 2
    assert code[-1] == "exit $lastexitcode"


def _stub_runner(root: Path, script: str) -> dict[str, str]:
    """Copy a wrapper next to a stub scripts/dev.py that echoes argv, writes to stderr and
    exits with the code given as exit=<n>. Returns an env without uv (wrapper uses python)."""
    shutil.copy(dev_mod.ROOT / script, root / script)
    (root / "scripts").mkdir()
    (root / "scripts" / "dev.py").write_text(
        "import sys\n"
        "print('ARGV=' + repr(sys.argv[1:]))\n"
        "sys.stderr.write('stub stderr line\\n')\n"
        "codes = [int(a[5:]) for a in sys.argv[1:] if a.startswith('exit=')]\n"
        "raise SystemExit(codes[0] if codes else 0)\n",
        encoding="utf-8",
    )
    system32 = Path(os.environ["SYSTEMROOT"]) / "System32"
    powershell_dir = system32 / "WindowsPowerShell" / "v1.0"
    path = os.pathsep.join([str(Path(sys.executable).parent), str(system32), str(powershell_dir)])
    return {**os.environ, "PATH": path}


def _powershell(root: Path, env: dict[str, str], command: str) -> CompletedProcess[str]:
    exe = Path(os.environ["SYSTEMROOT"]) / "System32" / "WindowsPowerShell" / "v1.0"
    return subprocess.run(  # noqa: S603
        [
            str(exe / "powershell.exe"),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"Set-Location -LiteralPath '{root}'; {command}; exit $LASTEXITCODE",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="runs the real Windows PowerShell")
@pytest.mark.parametrize(
    ("invocation", "code"),
    [
        (r".\dev.ps1 test exit=3", 3),
        (r".\dev.ps1 test exit=3 2>&1 | Tee-Object -FilePath log.txt", 3),
        (r".\dev.ps1 test exit=0 *> log.txt", 0),
        (r".\dev.ps1 test exit=4 *> log.txt", 4),
    ],
)
def test_dev_ps1_keeps_exit_code_when_output_is_redirected(
    tmp_path: Path, invocation: str, code: int
) -> None:
    env = _stub_runner(tmp_path, "dev.ps1")
    assert _powershell(tmp_path, env, invocation).returncode == code


@pytest.mark.skipif(sys.platform != "win32", reason="runs the real Windows PowerShell")
def test_dev_ps1_forwards_flags_that_look_like_common_parameters(tmp_path: Path) -> None:
    env = _stub_runner(tmp_path, "dev.ps1")
    proc = _powershell(
        tmp_path, env, r".\dev.ps1 test -p no:cacheprovider -v -d -ErrorAction x -k name exit=5"
    )
    assert proc.returncode == 5, proc.stderr
    expected = ["test", "-p", "no:cacheprovider", "-v", "-d", "-ErrorAction", "x", "-k", "name"]
    assert f"ARGV={[*expected, 'exit=5']!r}" in proc.stdout


@pytest.mark.parametrize(
    "name",
    [
        "celerybeat-schedule",
        "celerybeat-schedule.bak",
        "celerybeat-schedule.dat",
        "celerybeat-schedule.dir",
    ],
)
def test_celery_beat_schedule_files_are_ignored(name: str) -> None:
    """`dev-worker` writes beat's schedule into the repo root: one file on Linux, three dbm files
    on Windows (left untracked by the native Windows run). None may show up in `git status`."""
    git = shutil.which("git")
    assert git is not None
    proc = subprocess.run(  # noqa: S603
        [git, "check-ignore", "--no-index", "-q", name], cwd=dev_mod.ROOT, check=False
    )
    assert proc.returncode == 0, name


def test_run_success_exit_code() -> None:
    """Verify _run returns subprocess returncode 0 on successful execution."""
    with patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = CompletedProcess(args=["echo"], returncode=0)
        ret = _run(["echo", "hello"], check=True)
        assert ret == 0


def test_run_failure_exit_code_without_check() -> None:
    """Verify _run returns non-zero returncode when check=False."""
    with patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = CompletedProcess(args=["false"], returncode=42)
        ret = _run(["false"], check=False)
        assert ret == 42


def test_run_failure_exit_code_with_check() -> None:
    """Verify _run calls sys.exit when command fails and check=True."""
    with patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value = CompletedProcess(args=["false"], returncode=5)
        with pytest.raises(SystemExit) as exc_info:
            _run(["false"], check=True)
        assert exc_info.value.code == 5


def test_needs_resolution_unix() -> None:
    """Verify _needs_resolution on Unix behaves appropriately."""
    # POSIX path rules too: on a Windows host "/usr/bin/git" has no drive, so is not absolute.
    with patch("scripts.dev.IS_WINDOWS", False), patch("scripts.dev.Path", PurePosixPath):
        assert _needs_resolution("git") is True
        assert _needs_resolution("/usr/bin/git") is False
        assert _needs_resolution("./script.sh") is False


def test_needs_resolution_windows() -> None:
    """Verify _needs_resolution on Windows checks for executable extensions."""
    with patch("scripts.dev.IS_WINDOWS", True):
        assert _needs_resolution("git") is True
        assert _needs_resolution("git.exe") is False
        assert _needs_resolution("run.bat") is False
        assert _needs_resolution("npm.cmd") is False


def test_cmd_clean_removes_cache_directories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify cmd_clean removes cache directories and files without errors."""
    cache_dir = tmp_path / ".pytest_cache"
    cache_dir.mkdir()
    (cache_dir / "test_data.txt").write_text("cache", encoding="utf-8")

    cov_file = tmp_path / "coverage.xml"
    cov_file.write_text("<coverage/>", encoding="utf-8")

    with patch("scripts.dev.ROOT", tmp_path):
        code = cmd_clean([])
        assert code == 0
        assert not cache_dir.exists()
        assert not cov_file.exists()
        out = capsys.readouterr().out
        assert "Cleaned cache and build artifacts." in out


def test_cmd_datagen_defaults_to_load_and_passes_env(tmp_path: Path) -> None:
    """datagen loads by default, forwards options, and gets DATABASE_URL from .env."""
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql+psycopg://u:p@localhost/db\n", encoding="utf-8")
    with (
        patch("scripts.dev.ROOT", tmp_path),
        patch("scripts.dev._run", return_value=0) as mock_run,
    ):
        assert cmd_datagen(["--seed", "7"]) == 0
        cmd_args: list[str] = mock_run.call_args[0][0]
        assert cmd_args[-4:] == ["pbip_datagen", "load", "--seed", "7"]
        assert mock_run.call_args.kwargs["env"]["DATABASE_URL"].startswith("postgresql")

        cmd_datagen(["export", "--out", "data"])
        assert mock_run.call_args[0][0][-4:] == ["pbip_datagen", "export", "--out", "data"]


def test_ml_commands_target_the_mlflow_service_and_smoke_module() -> None:
    with patch("scripts.dev._run", return_value=0) as mock_run:
        dev_mod.cmd_ml_up([])
        assert mock_run.call_args[0][0] == [
            "docker", "compose", "--profile", "ml", "up", "-d", "--wait", "mlflow",
        ]  # fmt: skip
        dev_mod.cmd_mlflow_smoke(["--require-server"])
        assert mock_run.call_args[0][0][-2:] == ["pbip_ml.tracking_smoke", "--require-server"]


def test_perf_uses_native_k6_when_installed() -> None:
    cmd, env = dev_mod.build_k6_command("baseline.js", None, True, ["--quiet"])
    assert cmd == ["k6", "run", "infra/k6/baseline.js", "--quiet"]
    assert env == {"BASE_URL": "http://localhost:8000"}


def test_perf_falls_back_to_docker_with_host_networking_on_linux() -> None:
    with patch("scripts.dev.IS_WINDOWS", False), patch("scripts.dev.sys.platform", "linux"):
        cmd, env = dev_mod.build_k6_command("baseline.js", None, False, [])
    assert env == {}
    assert cmd[:4] == ["docker", "run", "--rm", "-i"]
    assert "BASE_URL=http://localhost:8000" in cmd and "--network" in cmd
    assert dev_mod.K6_IMAGE in cmd and cmd[-1] == "/scripts/baseline.js"


def test_perf_uses_host_docker_internal_on_docker_desktop() -> None:
    with patch("scripts.dev.IS_WINDOWS", True):
        cmd, _ = dev_mod.build_k6_command("baseline.js", None, False, [])
    assert "BASE_URL=http://host.docker.internal:8000" in cmd and "--network" not in cmd
    custom, _ = dev_mod.build_k6_command("baseline.js", "http://web:8080", False, [])
    assert "BASE_URL=http://web:8080" in custom


def test_k6_baseline_keeps_its_acceptance_thresholds() -> None:
    """Guard against silently weakening TASK-012's pass/fail criteria."""
    script = (dev_mod.ROOT / "infra" / "k6" / "baseline.js").read_text(encoding="utf-8")
    for required in (
        'http_req_failed: ["rate<0.001"]',
        'http_req_duration: ["p(95)<50"]',
        "/api/health/live",
        "/api/health/ready",
        "VUS || 50",
    ):
        assert required in script, required


def test_up_waits_for_healthy_services(capsys: pytest.CaptureFixture[str]) -> None:
    """`up` must not return while the API is still starting (first page load was a 502)."""
    with patch("scripts.dev._run", return_value=0) as mock_run:
        assert dev_mod.cmd_up([]) == 0
        assert mock_run.call_args[0][0] == ["docker", "compose", "up", "-d", "--build", "--wait"]
        dev_mod.cmd_up_all([])
        assert "--wait" in mock_run.call_args[0][0]
    assert "https://localhost:8443" in capsys.readouterr().out


def test_stack_commands_create_the_dev_tls_material_first() -> None:
    """The HTTPS stack (nginx :8443, PostgreSQL hostssl) cannot start without infra/certs."""
    for command in (dev_mod.cmd_up, dev_mod.cmd_up_all, dev_mod.cmd_infra_up):
        with patch("scripts.dev._run", return_value=0) as mock_run:
            command([])
        first = mock_run.call_args_list[0][0][0]
        assert first[-1].endswith("dev_certs.py"), command.__name__
        assert mock_run.call_args_list[-1][0][0][:2] == ["docker", "compose"]


def test_smoke_defaults_to_https_and_trusts_the_dev_ca(tmp_path: Path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("dummy", encoding="utf-8")
    with patch("scripts.dev.DEV_CA", ca), patch("scripts.dev._run", return_value=0) as mock_run:
        dev_mod.cmd_smoke([])
        assert mock_run.call_args[0][0][-1] == "https://localhost:8443"
        assert mock_run.call_args[1]["env"] == {"SSL_CERT_FILE": str(ca)}
        dev_mod.cmd_smoke(["https://staging.example"])
        assert mock_run.call_args[1]["env"] is None  # real hosts use the system trust store
