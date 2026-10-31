"""Every way the repository starts uvicorn keeps uvicorn's own access log off.

The app writes one structured access line without the query string (core/middleware.py,
AGENTS.md section 5). uvicorn's access line carries the raw query string, so any launch path
that leaves it on can leak tokens or emails into logs and CI artifacts (the fuzz workflow
uploaded such a log until TASK-085's follow-up).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
_SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "build", "htmlcov", "__pycache__", "mlruns"}
_SKIP_SUFFIXES = {".lock", ".json", ".png", ".svg", ".ico", ".mmdb", ".parquet", ".csv", ".ipynb"}
_THIS_FILE = Path(__file__).resolve()
_COMMAND_END = re.compile(r"[\])]|(?<![\\,])\n")


def _text_files() -> list[Path]:
    files: list[Path] = []
    for directory, subdirs, names in os.walk(ROOT):
        subdirs[:] = [d for d in subdirs if d not in _SKIP_DIRS]
        for name in names:
            path = Path(directory) / name
            if path.suffix not in _SKIP_SUFFIXES and path.resolve() != _THIS_FILE:
                files.append(path)
    return files


def _launch_commands(text: str) -> list[str]:
    """Each ``uvicorn`` mention that names an ASGI app (``module:app``), up to the end of its
    command: a closing bracket (Docker/Python argv lists) or a line end that is not continued
    with a backslash or a list comma."""
    commands: list[str] = []
    for match in re.finditer(r"\buvicorn\b", text):
        end = _COMMAND_END.search(text, match.end())
        command = text[match.start() : end.start() if end else len(text)]
        if re.search(r"\w:app\b", command) or command.startswith("uvicorn.run("):
            commands.append(command)
    return commands


def _launches() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in _text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        commands = _launch_commands(text)
        if commands:
            found[path.relative_to(ROOT).as_posix()] = commands
    return found


def test_command_extraction_handles_each_launch_style() -> None:
    assert _launch_commands('CMD ["uvicorn", "pbip_api.main:app", "--no-access-log"]\n') == [
        'uvicorn", "pbip_api.main:app", "--no-access-log"'
    ]
    assert _launch_commands('["uv", "uvicorn", "m:app",\n "--no-access-log", *args]') == [
        'uvicorn", "m:app",\n "--no-access-log", *args'
    ]
    assert _launch_commands("nohup uv run uvicorn m:app \\\n  --port 1 > log &\nnext") == [
        "uvicorn m:app \\\n  --port 1 > log &"
    ]
    assert _launch_commands("| API | FastAPI uvicorn reload server |\n") == []


def test_every_uvicorn_launch_disables_its_access_log() -> None:
    launches = _launches()
    # The scan must see the known launch paths, or it would pass by finding nothing.
    assert {"apps/api/Dockerfile", "scripts/dev.py", ".github/workflows/fuzz.yml"} <= set(launches)
    offenders = [
        f"{path}: {command!r}"
        for path, commands in launches.items()
        for command in commands
        if ("--no-access-log" not in command and "access_log=False" not in command)
        or "--access-log" in command
    ]
    assert offenders == []
