"""Every way the repository starts uvicorn keeps uvicorn's own access log off.

The app writes one structured access line without the query string (core/middleware.py,
AGENTS.md section 5). uvicorn's access line carries the raw query string, so any launch path
that leaves it on can leak tokens or emails into logs and CI artifacts (the fuzz workflow
uploaded such a log until TASK-085's follow-up).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
_SKIP_SUFFIXES = {".lock", ".json", ".png", ".svg", ".ico", ".mmdb", ".parquet", ".csv", ".ipynb"}
_THIS_FILE = Path(__file__).resolve()
_COMMAND_END = re.compile(r"[\])]|(?<![\\,])\n")


def _text_files(root: Path = ROOT) -> list[Path]:
    """The repository's tracked files only (``git ls-files``). Walking the directory also read
    untracked copies of the code: git worktrees that tooling creates under ``.claude/worktrees/``,
    mutmut's ``mutants/`` and virtualenvs, and reported their files as offenders (#329)."""
    git = shutil.which("git")
    assert git is not None, "the scan lists the repository's tracked files with git"
    listed = subprocess.run(  # noqa: S603 - fixed git command on the repository root
        [git, "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8")
    files: list[Path] = []
    for name in filter(None, listed.split("\0")):
        path = root / name
        if path.suffix not in _SKIP_SUFFIXES and path.resolve() != _THIS_FILE and path.is_file():
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


def test_scan_reads_only_tracked_files_not_worktree_copies(tmp_path: Path) -> None:
    """#329: a git worktree under ``.claude/worktrees/`` holds untracked copies of every launch file,
    which the directory walk reported as offenders. Only the repository's own files are scanned."""
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)  # noqa: S603 - fixed command
    (tmp_path / "Dockerfile").write_text('CMD ["uvicorn", "m:app", "--no-access-log"]\n')
    subprocess.run([git, "add", "Dockerfile"], cwd=tmp_path, check=True)  # noqa: S603 - fixed
    copy = tmp_path / ".claude" / "worktrees" / "agent-1"
    copy.mkdir(parents=True)
    (copy / "Dockerfile").write_text('CMD ["uvicorn", "m:app"]\n')

    assert _text_files(tmp_path) == [tmp_path / "Dockerfile"]
