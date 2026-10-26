"""Unit tests for scripts/normalize_secrets_baseline.py and the committed baseline.

Verifies:
- A Windows-written baseline (backslash paths, CRLF) is rewritten to the bytes Linux produces.
- No secret entry is dropped or altered (type, hash, line number) by the normalisation.
- The committed .secrets.baseline is already normalised (no OS-specific churn reaches main).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.normalize_secrets_baseline as nsb

pytestmark = pytest.mark.unit


def _entry(path: str, line: int) -> dict[str, object]:
    return {
        "type": "Secret Keyword",
        "filename": path,
        "hashed_secret": "0" * 40,
        "is_verified": False,
        "line_number": line,
    }


WINDOWS: dict[str, Any] = {
    "version": "1.5.0",
    "results": {
        "scripts\\seed_dev.py": [_entry("scripts\\seed_dev.py", 27)],
        "apps\\api\\x.py": [_entry("apps\\api\\x.py", 3), _entry("apps\\api\\x.py", 9)],
    },
    "generated_at": "2026-10-26T00:00:00Z",
}


def test_windows_baseline_is_rewritten_to_posix_lf(tmp_path: Path) -> None:
    baseline = tmp_path / ".secrets.baseline"
    baseline.write_bytes(json.dumps(WINDOWS, indent=2).replace("\n", "\r\n").encode())

    assert nsb.main(["hook", str(baseline)]) == 1

    raw = baseline.read_bytes().decode()
    assert "\r" not in raw and "\\\\" not in raw
    data = json.loads(raw)
    assert list(data["results"]) == ["apps/api/x.py", "scripts/seed_dev.py"]
    assert [e["line_number"] for e in data["results"]["apps/api/x.py"]] == [3, 9]
    assert all(e["filename"] == p for p, es in data["results"].items() for e in es)
    assert nsb.main(["hook", str(baseline)]) == 0  # idempotent


def _secrets(baseline: dict[str, Any]) -> list[tuple[Any, ...]]:
    results = baseline["results"]
    return sorted(
        (e["type"], e["hashed_secret"], e["line_number"]) for es in results.values() for e in es
    )


def test_normalisation_keeps_every_secret_entry() -> None:
    assert _secrets(nsb.normalize(WINDOWS)) == _secrets(WINDOWS)


def test_committed_baseline_is_normalised() -> None:
    raw = (nsb.ROOT / ".secrets.baseline").read_bytes().decode()
    assert raw == nsb.render(nsb.normalize(json.loads(raw)))
