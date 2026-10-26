"""Keep .secrets.baseline identical across operating systems (pre-commit hook).

detect-secrets on Windows writes result paths with backslashes ("apps\\api\\...") and CRLF when it
updates the baseline. The repository and CI (Linux) use forward slashes, so a Windows-written
baseline no longer matches its own entries there. This hook rewrites the paths with "/", keeps the
results sorted by path and writes LF, the same bytes detect-secrets produces on Linux. It changes
no secret entry (type, hash, line number, verified flag), so scanning is not weakened.

python scripts/normalize_secrets_baseline.py [path]   # exit 1 if it rewrote the file
"""

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def normalize(baseline: dict[str, Any]) -> dict[str, Any]:
    """Return the baseline with POSIX result paths, sorted by path."""
    results: dict[str, list[dict[str, Any]]] = baseline.get("results", {})
    fixed: dict[str, list[dict[str, Any]]] = {}
    for path, entries in results.items():
        posix = path.replace("\\", "/")
        fixed[posix] = [{**entry, "filename": posix} for entry in entries]
    return {**baseline, "results": dict(sorted(fixed.items()))}


def render(baseline: dict[str, Any]) -> str:
    """Serialise exactly like detect-secrets (2-space indent, trailing newline)."""
    return json.dumps(baseline, indent=2) + "\n"


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else ROOT / ".secrets.baseline"
    current = path.read_bytes().decode("utf-8")
    wanted = render(normalize(json.loads(current)))
    if current == wanted:
        return 0
    path.write_bytes(wanted.encode("utf-8"))
    sys.stdout.write(f"Normalised {path.name} (POSIX paths, LF). Please `git add` it again.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
