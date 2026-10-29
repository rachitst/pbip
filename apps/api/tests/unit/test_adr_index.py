"""ADRs are cited by number (``ADR-0008``) from code and docs, so a number must name one file only.

Regression: the TASK-073 passkeys ADR was first committed as ``0008-...`` next to the existing
``0008-ledger-keyset-cursor.md``.
"""

import re
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ADR_DIR = Path(__file__).resolve().parents[4] / "docs" / "adr"
_NAME = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")


def test_every_adr_file_is_numbered_and_numbers_are_unique() -> None:
    files = sorted(p.name for p in ADR_DIR.glob("*.md"))
    assert files, "no ADRs found"
    unnumbered = [name for name in files if not _NAME.match(name)]
    assert not unnumbered, f"ADR files must be NNNN-kebab-title.md: {unnumbered}"
    counts = Counter(name[:4] for name in files)
    duplicates = sorted(number for number, count in counts.items() if count > 1)
    assert not duplicates, f"duplicate ADR numbers: {duplicates}"


def test_every_adr_heading_matches_its_file_number_and_has_a_status() -> None:
    for path in sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")):
        if path.name.startswith("0000-"):
            continue  # the template
        head = path.read_text(encoding="utf-8").splitlines()[:6]
        assert head[0].startswith(f"# ADR {path.name[:4]}"), (path.name, head[0])
        assert any(line.startswith("- Status:") for line in head), path.name
