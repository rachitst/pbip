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

from pathlib import Path
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
    parsed (always 0 there), so dev.cmd exited 0 even when the command failed."""
    lines = [
        ln.strip() for ln in (dev_mod.ROOT / "dev.cmd").read_text(encoding="utf-8").splitlines()
    ]
    depth = 0
    for line in lines:
        if depth > 0 and not line.upper().startswith("REM"):
            assert "%ERRORLEVEL%" not in line.upper(), line
        depth += line.count("(") - line.count(")")
    runs = [i for i, line in enumerate(lines) if line.endswith('scripts\\dev.py" %*')]
    assert len(runs) == 2
    for index in runs:  # bare "exit /b" keeps the ERRORLEVEL of the command that just ran
        assert lines[index + 1].lower() == "exit /b"
    assert not any(line.startswith(":") for line in lines)  # no labels (LF-stored file)


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
    with patch("scripts.dev.IS_WINDOWS", False):
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
