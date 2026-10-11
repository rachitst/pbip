"""Unit tests for PBIP environment doctor diagnostics (scripts/dev.py).

Verifies:
- Host Python version check (>= 3.12 returns [OK], < 3.12 returns [WARN]).
- Required CLI tooling detection and missing tool reporting.
- Docker daemon and compose plugin status checks (healthy, not running, missing, timed out).
- Project workspace configuration checks (.env, .venv, node_modules).
- Port check probing and busy/free status reporting.
- OS-specific installation instructions (Windows winget, macOS brew, Linux apt).
- cmd_doctor orchestrates all checks and preserves exit code 0.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.dev import (
    _check_docker_status,
    _check_ports,
    _check_python_version,
    _check_tools,
    _check_workspace_state,
    _print_instructions,
    cmd_doctor,
)

pytestmark = pytest.mark.unit


def test_check_python_version_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_python_version returns True and displays [OK] when >= 3.12."""
    fake_version = (3, 12, 4, "final", 0)
    with (
        patch("sys.version_info", fake_version),
        patch("sys.executable", "/usr/bin/python3"),
    ):
        result = _check_python_version()
        assert result is True
        out = capsys.readouterr().out
        assert "Host Python      : [OK]      3.12.4 (/usr/bin/python3)" in out


def test_check_python_version_warn(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_python_version returns False and displays [WARN] when < 3.12."""
    fake_version = (3, 11, 8, "final", 0)
    with (
        patch("sys.version_info", fake_version),
        patch("sys.executable", "/usr/bin/python3.11"),
    ):
        result = _check_python_version()
        assert result is False
        out = capsys.readouterr().out
        assert "Host Python      : [WARN]    3.11.8 (/usr/bin/python3.11)" in out
        assert "Python 3.12+ required for PBIP" in out


def test_check_tools_all_present(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_tools returns empty list when all tools are found."""

    def mock_find(name: str) -> str:
        return f"/mock/bin/{name}"

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        return MagicMock(returncode=0, stdout=f"{cmd[0]} version 1.0.0\n")

    with (
        patch("scripts.dev._find_tool", side_effect=mock_find),
        patch("subprocess.run", side_effect=mock_run),
    ):
        missing = _check_tools()
        assert missing == []
        out = capsys.readouterr().out
        assert "[OK]" in out
        assert "[MISSING]" not in out


def test_check_tools_with_missing(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_tools flags missing tools as [MISSING] and returns their names."""

    def mock_find(name: str) -> str | None:
        if name in ("docker", "gh"):
            return None
        return f"/mock/bin/{name}"

    with patch("scripts.dev._find_tool", side_effect=mock_find):
        missing = _check_tools()
        assert "docker" in missing
        assert "gh" in missing
        out = capsys.readouterr().out
        assert "  [MISSING] docker" in out
        assert "  [MISSING] gh" in out


def test_check_docker_status_not_installed(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_docker_status reports missing Docker when not found."""
    with patch("scripts.dev._find_tool", return_value=None):
        _check_docker_status()
        out = capsys.readouterr().out
        assert "  [MISSING] Docker is not installed." in out


def test_check_docker_status_running(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_docker_status reports OK when daemon and compose are responsive."""

    def mock_subproc(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "info":
            return MagicMock(returncode=0, stdout="Docker info OK")
        if cmd[1:] == ["compose", "version"]:
            return MagicMock(returncode=0, stdout="Docker Compose version v2.29.1")
        return MagicMock(returncode=0, stdout="")

    with (
        patch("scripts.dev._find_tool", return_value="/mock/bin/docker"),
        patch("subprocess.run", side_effect=mock_subproc),
    ):
        _check_docker_status()
        out = capsys.readouterr().out
        assert "  [OK]      Docker daemon is running and responsive." in out
        assert "  [OK]      Docker Compose: Docker Compose version v2.29.1" in out


def test_check_docker_status_stopped_daemon(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_docker_status warns when daemon is down."""

    def mock_subproc(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "info":
            return MagicMock(returncode=1, stderr="Cannot connect to the Docker daemon")
        return MagicMock(returncode=0, stdout="compose ok")

    with (
        patch("scripts.dev._find_tool", return_value="/mock/bin/docker"),
        patch("subprocess.run", side_effect=mock_subproc),
    ):
        _check_docker_status()
        out = capsys.readouterr().out
        assert "  [WARN]    Docker daemon is NOT running or accessible." in out


def test_check_docker_status_timeout(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_docker_status handles daemon timeout gracefully."""

    def mock_subproc(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "info":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
        return MagicMock(returncode=0, stdout="compose ok")

    with (
        patch("scripts.dev._find_tool", return_value="/mock/bin/docker"),
        patch("subprocess.run", side_effect=mock_subproc),
    ):
        _check_docker_status()
        out = capsys.readouterr().out
        assert "  [WARN]    Docker daemon check timed out." in out


def test_check_workspace_state_all_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify _check_workspace_state reports [OK] when all components exist."""
    (tmp_path / ".env").write_text("APP_ENV=local\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / "apps" / "web" / "node_modules").mkdir(parents=True)

    with patch("scripts.dev.ROOT", tmp_path):
        _check_workspace_state()
        out = capsys.readouterr().out
        assert "  [OK]      .env file exists." in out
        assert "  [OK]      Python virtualenv (.venv) exists." in out
        assert "  [OK]      Web node_modules exists." in out


def test_check_workspace_state_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_workspace_state reports [WARN] when files are absent."""
    with patch("scripts.dev.ROOT", tmp_path):
        _check_workspace_state()
        out = capsys.readouterr().out
        assert "  [WARN]    .env file missing" in out
        assert "  [WARN]    Python virtualenv (.venv) missing" in out
        assert "  [WARN]    apps/web/node_modules missing" in out


def test_check_ports_busy_and_free(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _check_ports identifies busy vs free ports using mocked socket."""

    def mock_connect_ex(addr: tuple[str, int]) -> int:
        _host, port = addr
        if port == 5432:
            return 0  # 0 indicates connection succeeded -> port is IN USE
        return 111  # non-zero indicates connection failed -> port is FREE

    mock_sock = MagicMock()
    mock_sock.connect_ex.side_effect = mock_connect_ex
    mock_sock.__enter__.return_value = mock_sock

    with patch("socket.socket", return_value=mock_sock):
        _check_ports()
        out = capsys.readouterr().out
        assert "Local Port Diagnostics (informational):" in out
        assert "[IN USE]  Port  5432 (PostgreSQL (db))" in out
        assert "[FREE]    Port  6379 (Redis (cache/queue))" in out
        assert "[FREE]    Port  8000 (API (uvicorn))" in out


def test_print_instructions_windows_guidance(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _print_instructions displays winget commands on Windows."""
    with patch("scripts.dev.IS_WINDOWS", True):
        _print_instructions(["uv", "docker"])
        out = capsys.readouterr().out
        assert "Windows (PowerShell as Administrator):" in out
        assert "winget install --id Astral-sh.uv" in out
        assert "winget install --id Docker.DockerDesktop" in out


def test_print_instructions_macos_guidance(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _print_instructions displays brew commands on macOS."""
    with (
        patch("scripts.dev.IS_WINDOWS", False),
        patch("sys.platform", "darwin"),
    ):
        _print_instructions(["uv", "node"])
        out = capsys.readouterr().out
        assert "macOS (Homebrew):" in out
        assert "brew install uv node git gh" in out


def test_print_instructions_linux_guidance(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _print_instructions displays apt/curl commands on Linux."""
    with (
        patch("scripts.dev.IS_WINDOWS", False),
        patch("sys.platform", "linux"),
    ):
        _print_instructions(["uv", "git"])
        out = capsys.readouterr().out
        assert "Ubuntu / Linux:" in out
        assert "sudo apt update && sudo apt install -y git curl" in out


def test_print_instructions_empty_does_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _print_instructions produces no output when no tools are missing."""
    _print_instructions([])
    out = capsys.readouterr().out
    assert out == ""


def test_cmd_doctor_full_run_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify cmd_doctor executes complete diagnostic suite and returns exit code 0."""
    with (
        patch("scripts.dev._check_tools", return_value=[]),
        patch("scripts.dev._check_docker_status"),
        patch("scripts.dev._check_workspace_state"),
        patch("scripts.dev._check_ports"),
    ):
        code = cmd_doctor([])
        assert code == 0
        out = capsys.readouterr().out
        assert "PBIP Environment Diagnostics (dev doctor)" in out
        assert "Host Python      :" in out
        assert "Doctor check completed." in out
