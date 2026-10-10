#!/usr/bin/env python3
"""PBIP Unified Developer Command Runner.

Works identically on Ubuntu/Linux, macOS, and native Windows PowerShell/CMD.
Usage:
    python scripts/dev.py <command> [args...]
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = sys.platform == "win32"


def _needs_resolution(executable: str) -> bool:
    """Check if executable needs PATH lookup resolution."""
    if IS_WINDOWS and not executable.endswith((".exe", ".cmd", ".bat")):
        return True
    return not IS_WINDOWS and not Path(executable).is_absolute() and not executable.startswith(".")


def _run(
    cmd: list[str],
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> int:
    """Run a subprocess command, streaming output to stdout/stderr."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    # Ensure local bin and NVM node paths are in PATH
    extra_paths = [
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".cargo" / "bin"),
    ]
    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_dir.exists():
        for ver_dir in sorted(nvm_dir.glob("v*"), reverse=True):
            extra_paths.append(str(ver_dir / "bin"))
    existing_path = merged_env.get("PATH", "")
    merged_env["PATH"] = os.pathsep.join([*extra_paths, existing_path])

    executable = cmd[0]
    if _needs_resolution(executable):
        found = shutil.which(executable, path=merged_env["PATH"])
        if found:
            cmd[0] = found

    sys.stdout.flush()
    sys.stderr.flush()
    proc = subprocess.run(cmd, cwd=str(cwd), env=merged_env, check=False)
    if check and proc.returncode != 0:
        sys.exit(proc.returncode)
    return proc.returncode


def _find_tool(name: str) -> str | None:
    """Find executable in PATH or common user installation directories."""
    path = shutil.which(name)
    if path:
        return path
    if IS_WINDOWS:
        candidates = [
            Path.home() / ".cargo" / "bin" / f"{name}.exe",
            Path.home() / "AppData" / "Local" / "Programs" / name / f"{name}.exe",
            Path.home() / "AppData" / "Roaming" / "npm" / f"{name}.cmd",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
    else:
        candidates = [
            Path.home() / ".local" / "bin" / name,
            Path.home() / ".cargo" / "bin" / name,
        ]
        nvm_dir = Path.home() / ".nvm" / "versions" / "node"
        if nvm_dir.exists():
            for ver_dir in sorted(nvm_dir.glob("v*"), reverse=True):
                candidates.append(ver_dir / "bin" / name)
        for c in candidates:
            if c.exists():
                return str(c)
    return None


def cmd_env(args: list[str]) -> int:
    """Create .env with freshly generated local secrets."""
    return _run([sys.executable, str(ROOT / "scripts" / "init_env.py"), *args])


def cmd_setup(args: list[str]) -> int:
    """One-time setup: .env, Python + Node deps, git hooks."""
    sys.stdout.write("==> Step 1: Initializing .env...\n")
    cmd_env(args)

    sys.stdout.write("==> Step 2: Installing Python dependencies via uv sync...\n")
    _run(["uv", "sync", "--all-packages"])

    sys.stdout.write("==> Step 3: Installing Node dependencies in apps/web...\n")
    _run(["npm", "ci"], cwd=ROOT / "apps" / "web")

    sys.stdout.write("==> Step 4: Installing pre-commit hooks...\n")
    _run(
        [
            "uv",
            "run",
            "pre-commit",
            "install",
            "--hook-type",
            "pre-commit",
            "--hook-type",
            "commit-msg",
            "--hook-type",
            "pre-push",
        ]
    )
    sys.stdout.write(
        "Setup complete! You can run `make doctor` or `python scripts/dev.py doctor`.\n"
    )
    return 0


def _check_tools() -> list[str]:
    """Check required CLI tools and return missing tool names."""
    tools = [
        ("git", "Git version control", "https://git-scm.com"),
        ("uv", "Astral uv Python package manager", "https://astral.sh/uv"),
        ("node", "Node.js (v20+ or v22+)", "https://nodejs.org"),
        ("npm", "Node package manager", "https://nodejs.org"),
        ("docker", "Docker container engine", "https://docs.docker.com/get-docker/"),
        ("gh", "GitHub CLI (for backlog & PRs)", "https://cli.github.com"),
    ]
    sys.stdout.write("Installed Tooling:\n")
    missing_tools: list[str] = []
    for tool_name, desc, url in tools:
        found = _find_tool(tool_name)
        if found:
            ver_text = "detected"
            try:
                out = subprocess.run(
                    [found, "--version"], capture_output=True, text=True, check=False
                )
                if out.returncode == 0:
                    ver_text = out.stdout.strip().splitlines()[0]
            except (subprocess.SubprocessError, OSError):
                ver_text = "error checking version"
            sys.stdout.write(f"  [OK]      {tool_name:8} : {ver_text} ({found})\n")
        else:
            sys.stdout.write(f"  [MISSING] {tool_name:8} : {desc} -> {url}\n")
            missing_tools.append(tool_name)
    return missing_tools


def _check_docker_status() -> None:
    """Verify Docker daemon and compose readiness."""
    sys.stdout.write("\nDocker & Compose Status:\n")
    docker_bin = _find_tool("docker")
    if not docker_bin:
        sys.stdout.write("  [MISSING] Docker is not installed.\n")
        return

    try:
        res = subprocess.run(
            [docker_bin, "info"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if res.returncode == 0:
            sys.stdout.write("  [OK]      Docker daemon is running and responsive.\n")
        else:
            sys.stdout.write("  [WARN]    Docker daemon is NOT running or accessible.\n")
    except subprocess.TimeoutExpired:
        sys.stdout.write("  [WARN]    Docker daemon check timed out.\n")
    except Exception as exc:
        sys.stdout.write(f"  [WARN]    Failed to check Docker daemon: {exc}\n")

    try:
        res = subprocess.run(
            [docker_bin, "compose", "version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            sys.stdout.write(f"  [OK]      Docker Compose: {res.stdout.strip()}\n")
        else:
            sys.stdout.write("  [WARN]    Docker Compose v2 plugin is missing.\n")
    except Exception:
        sys.stdout.write("  [WARN]    Docker Compose check failed.\n")


def _check_workspace_state() -> None:
    """Check required configuration files and installed dependencies."""
    sys.stdout.write("\nProject Workspace State:\n")
    env_file = ROOT / ".env"
    if env_file.exists():
        sys.stdout.write("  [OK]      .env file exists.\n")
    else:
        sys.stdout.write("  [WARN]    .env file missing (run `make env`).\n")

    venv_dir = ROOT / ".venv"
    if venv_dir.exists():
        sys.stdout.write("  [OK]      Python virtualenv (.venv) exists.\n")
    else:
        sys.stdout.write("  [WARN]    Python virtualenv (.venv) missing (run `uv sync`).\n")

    node_modules = ROOT / "apps" / "web" / "node_modules"
    if node_modules.exists():
        sys.stdout.write("  [OK]      Web node_modules exists.\n")
    else:
        sys.stdout.write(
            "  [WARN]    apps/web/node_modules missing (run `cd apps/web && npm ci`).\n"
        )


def _check_ports() -> None:
    """Check status of standard PBIP local ports."""
    sys.stdout.write("\nLocal Port Diagnostics (informational):\n")
    ports = [
        (5432, "PostgreSQL (db)"),
        (6379, "Redis (cache/queue)"),
        (8000, "API (uvicorn)"),
        (5173, "Web (Vite dev)"),
        (8080, "Web/API proxy (Docker full stack)"),
        (3000, "Grafana"),
        (9090, "Prometheus"),
        (5000, "MLflow"),
    ]
    for port, label in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            is_busy = s.connect_ex(("127.0.0.1", port)) == 0
        status_label = "[IN USE]" if is_busy else "[FREE]"
        sys.stdout.write(f"  {status_label:9} Port {port:5} ({label})\n")


def _print_instructions(missing_tools: list[str]) -> None:
    """Print OS-tailored installation steps for missing tools."""
    if not missing_tools:
        return
    sys.stdout.write("\nActionable Installation Steps for missing tools:\n")
    if IS_WINDOWS:
        sys.stdout.write(
            "Windows (PowerShell as Administrator):\n"
            "  winget install --id Astral-sh.uv\n"
            "  winget install --id OpenJS.NodeJS.LTS\n"
            "  winget install --id Git.Git\n"
            "  winget install --id Docker.DockerDesktop\n"
            "  winget install --id GitHub.cli\n"
        )
    elif sys.platform == "darwin":
        sys.stdout.write(
            "macOS (Homebrew):\n  brew install uv node git gh\n  brew install --cask docker\n"
        )
    else:
        sys.stdout.write(
            "Ubuntu / Linux:\n"
            "  sudo apt update && sudo apt install -y git curl\n"
            "  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
            "  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -\n"
            "  sudo apt install -y nodejs gh\n"
        )


def cmd_doctor(_args: list[str]) -> int:
    """Run environment and platform diagnostics."""
    sys.stdout.write("PBIP Environment Diagnostics (dev doctor)\n")
    sys.stdout.write("=========================================\n\n")

    os_name = platform.system()
    os_release = platform.release()
    sys.stdout.write(f"Operating System : {os_name} {os_release} ({sys.platform})\n")
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    sys.stdout.write(f"Host Python      : {py_ver} ({sys.executable})\n\n")

    missing = _check_tools()
    _check_docker_status()
    _check_workspace_state()
    _check_ports()
    _print_instructions(missing)

    sys.stdout.write("\nDoctor check completed.\n")
    return 0


def cmd_infra_up(_args: list[str]) -> int:
    """Start PostgreSQL and Redis in Docker."""
    return _run(["docker", "compose", "up", "-d", "--wait", "db", "redis"])


def cmd_infra_down(_args: list[str]) -> int:
    """Stop PostgreSQL and Redis."""
    return _run(["docker", "compose", "stop", "db", "redis"])


def cmd_up(args: list[str]) -> int:
    """Build and start the full PBIP Docker stack."""
    return _run(["docker", "compose", "up", "-d", "--build", *args])


def cmd_up_all(args: list[str]) -> int:
    """Full stack + monitoring + MLflow."""
    return _run(
        [
            "docker",
            "compose",
            "--profile",
            "monitoring",
            "--profile",
            "ml",
            "up",
            "-d",
            "--build",
            *args,
        ]
    )


def cmd_down(args: list[str]) -> int:
    """Stop all Docker containers."""
    return _run(["docker", "compose", "--profile", "monitoring", "--profile", "ml", "down", *args])


def cmd_logs(args: list[str]) -> int:
    """Tail Docker API logs."""
    service = args[0] if args else "api"
    return _run(["docker", "compose", "logs", "-f", service])


def cmd_migrate(args: list[str]) -> int:
    """Apply Alembic migrations to database."""
    return _run(["uv", "run", "alembic", "-c", "apps/api/alembic.ini", "upgrade", "head", *args])


def cmd_seed(args: list[str]) -> int:
    """Seed demo users, accounts and MFA staff."""
    return _run(["uv", "run", "python", "scripts/seed_dev.py", *args])


def cmd_dev_api(args: list[str]) -> int:
    """Run FastAPI development server with auto-reload on :8000."""
    return _run(["uv", "run", "uvicorn", "pbip_api.main:app", "--reload", "--port", "8000", *args])


def cmd_dev_worker(args: list[str]) -> int:
    """Run Celery worker and beat. Uses --pool=solo on Windows."""
    cmd = [
        "uv",
        "run",
        "celery",
        "-A",
        "pbip_api.worker.celery_app",
        "worker",
        "--beat",
        "--loglevel=INFO",
    ]
    if IS_WINDOWS:
        cmd.append("--pool=solo")
    cmd.extend(args)
    return _run(cmd)


def cmd_dev_web(args: list[str]) -> int:
    """Run frontend development server (Vite) on :5173."""
    return _run(["npm", "run", "dev", "--", *args], cwd=ROOT / "apps" / "web")


def cmd_web_lint(_args: list[str]) -> int:
    """Run prettier format check and eslint for apps/web (used by pre-commit)."""
    _run(["npm", "run", "format:check", "--silent"], cwd=ROOT / "apps" / "web")
    _run(["npm", "run", "lint", "--silent"], cwd=ROOT / "apps" / "web")
    return 0


def cmd_lint(args: list[str]) -> int:
    """Lint and format-check Python and TypeScript."""
    sys.stdout.write("==> Python ruff check...\n")
    _run(["uv", "run", "ruff", "check", ".", *args])
    sys.stdout.write("==> Python ruff format check...\n")
    _run(["uv", "run", "ruff", "format", "--check", ".", *args])
    sys.stdout.write("==> Web eslint...\n")
    _run(["npm", "run", "lint"], cwd=ROOT / "apps" / "web")
    sys.stdout.write("==> Web prettier format check...\n")
    _run(["npm", "run", "format:check"], cwd=ROOT / "apps" / "web")
    return 0


def cmd_fmt(args: list[str]) -> int:
    """Auto-format Python and TypeScript code."""
    sys.stdout.write("==> Formatting Python with ruff...\n")
    _run(["uv", "run", "ruff", "check", "--fix", ".", *args])
    _run(["uv", "run", "ruff", "format", ".", *args])
    sys.stdout.write("==> Formatting Web with prettier...\n")
    _run(["npm", "run", "format"], cwd=ROOT / "apps" / "web")
    return 0


def cmd_typecheck(args: list[str]) -> int:
    """Run strict typechecking with mypy and tsc."""
    sys.stdout.write("==> Typechecking Python (mypy strict)...\n")
    _run(["uv", "run", "mypy", "apps/api/src", "ml/src", "data-gen/src", *args])
    sys.stdout.write("==> Typechecking TypeScript (tsc)...\n")
    _run(["npm", "run", "typecheck"], cwd=ROOT / "apps" / "web")
    return 0


def cmd_imports(args: list[str]) -> int:
    """Enforce architectural module boundaries using import-linter."""
    return _run(["uv", "run", "lint-imports", *args])


def cmd_security(args: list[str]) -> int:
    """Run static security checks, pip-audit and npm audit."""
    sys.stdout.write("==> Running Bandit SAST scan...\n")
    _run(
        [
            "uv",
            "run",
            "bandit",
            "-q",
            "-r",
            "apps/api/src",
            "ml/src",
            "data-gen/src",
            "-ll",
            *args,
        ]
    )

    sys.stdout.write("==> Running Python dependency vulnerability audit (pip-audit)...\n")
    temp_req = Path(tempfile.gettempdir()) / f"pbip-req-{os.getpid()}.txt"
    try:
        _run(
            [
                "uv",
                "export",
                "--frozen",
                "--all-packages",
                "--no-hashes",
                "--no-emit-workspace",
                "-o",
                str(temp_req),
            ]
        )
        _run(["uv", "run", "pip-audit", "-r", str(temp_req), "--no-deps"])
    finally:
        if temp_req.exists():
            temp_req.unlink(missing_ok=True)

    sys.stdout.write("==> Running Web npm audit...\n")
    _run(["npm", "audit", "--audit-level=high"], cwd=ROOT / "apps" / "web")
    return 0


def cmd_openapi(args: list[str]) -> int:
    """Regenerate OpenAPI contract and TypeScript types."""
    _run(["uv", "run", "python", "scripts/export_openapi.py", *args])
    _run(["npm", "run", "gen:api"], cwd=ROOT / "apps" / "web")
    return 0


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """Parse a single line from a .env file, returning (key, value) or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].strip()
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    if len(value) >= 2 and (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
    ):
        quote_char = value[0]
        unquoted = value[1:-1]
        if quote_char == '"':
            unquoted = (
                unquoted.replace(r"\n", "\n")
                .replace(r"\t", "\t")
                .replace(r"\"", '"')
                .replace(r"\\", "\\")
            )
        return key, unquoted
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return key, value


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse key-value pairs from a .env file."""
    if not path.is_file():
        return {}
    env_vars: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed:
            env_vars[parsed[0]] = parsed[1]
    return env_vars


_DEFAULT_TEST_PASSWORD = "pbip_local_password"  # noqa: S105  # pragma: allowlist secret


def derive_test_database_url(
    user: str = "pbip",
    password: str = _DEFAULT_TEST_PASSWORD,
    db: str = "pbip",
    host: str = "localhost",
    port: int = 5432,
) -> str:
    """Derive PostgreSQL test database URL, safely encoding credentials with special characters."""
    encoded_user = quote(user, safe="")
    encoded_password = quote(password, safe="")
    target_db = f"{db}_test"
    return f"postgresql+psycopg://{encoded_user}:{encoded_password}@{host}:{port}/{target_db}"


def build_test_env(
    env_path: Path = ROOT / ".env",
    explicit_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the environment dictionary for running tests.

    Loads the project's .env file, respects explicitly supplied variables,
    and derives TEST_DATABASE_URL targeting localhost:5432/${POSTGRES_DB}_test
    if absent. Never prints credentials or secrets.
    """
    current_env = os.environ if explicit_env is None else explicit_env
    file_env = parse_env_file(env_path)

    test_env: dict[str, str] = dict(file_env)
    test_env.update(current_env)

    if not test_env.get("TEST_DATABASE_URL"):
        user = test_env.get("POSTGRES_USER", "pbip")
        password = test_env.get("POSTGRES_PASSWORD", "pbip_local_password")
        db = test_env.get("POSTGRES_DB", "pbip")
        test_env["TEST_DATABASE_URL"] = derive_test_database_url(
            user=user,
            password=password,
            db=db,
            host="localhost",
            port=5432,
        )

    return test_env


def cmd_test(args: list[str]) -> int:
    """Run backend and ML test suite with coverage."""
    return _run(["uv", "run", "pytest", "--cov", *args], env=build_test_env())


def cmd_test_unit(args: list[str]) -> int:
    """Run fast unit and security tests (no DB required)."""
    return _run(
        ["uv", "run", "pytest", "-m", "unit or security and not integration", "-q", *args],
        env=build_test_env(),
    )


def cmd_test_int(args: list[str]) -> int:
    """Run integration tests against PostgreSQL."""
    return _run(["uv", "run", "pytest", "-m", "integration", "-q", *args], env=build_test_env())


def cmd_test_web(args: list[str]) -> int:
    """Run Vitest frontend tests."""
    return _run(["npm", "test"], cwd=ROOT / "apps" / "web")


def cmd_test_e2e(args: list[str]) -> int:
    """Run Playwright E2E tests against running stack."""
    _run(["npx", "playwright", "install", "--with-deps", "chromium"], cwd=ROOT / "apps" / "web")
    return _run(["npm", "run", "test:e2e"], cwd=ROOT / "apps" / "web")


def cmd_smoke(args: list[str]) -> int:
    """Run post-deploy smoke test."""
    url = args[0] if args else "http://localhost:8080"
    extra = args[1:]
    return _run([sys.executable, str(ROOT / "scripts" / "smoke_test.py"), url, *extra])


def cmd_check(args: list[str]) -> int:
    """Run everything CI runs: lint, typecheck, imports, security, tests, openapi."""
    sys.stdout.write("\n=========================================\n")
    sys.stdout.write("  PBIP Full Verification Gate (make check)\n")
    sys.stdout.write("=========================================\n\n")

    cmd_lint(args)
    cmd_typecheck(args)
    cmd_imports(args)
    cmd_security(args)
    cmd_test(args)
    cmd_test_web(args)
    _run(["uv", "run", "python", "scripts/export_openapi.py", "--check"])

    sys.stdout.write("\n[SUCCESS] All CI checks passed!\n")
    return 0


def cmd_clean(_args: list[str]) -> int:
    """Remove cache directories and build output cross-platform."""
    patterns = [
        ROOT / ".pytest_cache",
        ROOT / ".mypy_cache",
        ROOT / ".ruff_cache",
        ROOT / "htmlcov",
        ROOT / "coverage.xml",
        ROOT / "apps" / "web" / "dist",
        ROOT / "apps" / "web" / "coverage",
        ROOT / ".coverage",
    ]
    for path in patterns:
        if path.is_file():
            path.unlink(missing_ok=True)
            sys.stdout.write(f"Removed file: {path.relative_to(ROOT)}\n")
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            sys.stdout.write(f"Removed directory: {path.relative_to(ROOT)}\n")
    sys.stdout.write("Cleaned cache and build artifacts.\n")
    return 0


COMMANDS = {
    "help": "Show available commands and usage",
    "doctor": "Run diagnostic checks for platform, tools, and ports",
    "setup": "One-time setup: .env, Python + Node deps, git hooks",
    "env": "Create .env with freshly generated secrets",
    "infra-up": "Start PostgreSQL and Redis in Docker",
    "infra-down": "Stop PostgreSQL and Redis",
    "up": "Build and start full PBIP stack (web on :8080)",
    "up-all": "Full stack + Prometheus, Grafana, MLflow",
    "down": "Stop all Docker stack services",
    "logs": "Tail Docker API service logs",
    "migrate": "Apply Alembic database migrations",
    "seed": "Seed demo users, accounts and staff",
    "dev-api": "Run FastAPI backend on :8000 with auto-reload",
    "dev-worker": "Run Celery worker and beat (solo pool on Windows)",
    "dev-web": "Run Vite React frontend on :5173",
    "web-lint": "Run prettier and eslint checks in apps/web",
    "lint": "Run ruff and eslint checks",
    "fmt": "Auto-format Python and TypeScript code",
    "typecheck": "Run strict typechecking (mypy + tsc)",
    "imports": "Enforce architectural boundaries (import-linter)",
    "security": "Run Bandit SAST, pip-audit, and npm audit",
    "openapi": "Regenerate OpenAPI contract and TS client",
    "test": "Run all Python & ML tests with coverage",
    "test-unit": "Run fast unit tests (no database needed)",
    "test-int": "Run integration tests (requires PostgreSQL)",
    "test-web": "Run Vitest frontend tests",
    "test-e2e": "Run Playwright end-to-end tests",
    "smoke": "Run smoke test against localhost:8080 or custom URL",
    "check": "Run full CI verification suite locally",
    "clean": "Remove cache files and build outputs",
}


def cmd_help(_args: list[str]) -> int:
    """Print command overview."""
    sys.stdout.write("PBIP Developer Command Runner (scripts/dev.py)\n\n")
    sys.stdout.write("Usage: python scripts/dev.py <command> [arguments...]\n")
    sys.stdout.write("       make <command>         (on Linux/macOS)\n")
    sys.stdout.write("       .\\dev.ps1 <command>     (on Windows PowerShell)\n\n")
    sys.stdout.write("Available Commands:\n")
    for name, desc in COMMANDS.items():
        sys.stdout.write(f"  {name:14} : {desc}\n")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        return cmd_help(sys.argv[2:])

    action = sys.argv[1]
    extra = sys.argv[2:]

    handler_name = f"cmd_{action.replace('-', '_')}"
    handler = globals().get(handler_name)
    if handler and callable(handler):
        return handler(extra)  # type: ignore[no-any-return]

    sys.stderr.write(
        f"Unknown command: '{action}'. Run `python scripts/dev.py help` for commands.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
