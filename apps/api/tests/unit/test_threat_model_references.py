"""The threat model's evidence must exist: every cited test and code path resolves in the repo.

A threat model that cites a renamed or invented test silently overstates the controls (TASK-028 review found
claims of this kind). This keeps `docs/security/threat-model.md` honest as the code moves.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
THREAT_MODEL = ROOT / "docs" / "security" / "threat-model.md"
TEST_DIRS = [ROOT / "apps" / "api" / "tests", ROOT / "ml" / "tests", ROOT / "data-gen" / "tests"]
_API = ROOT / "apps" / "api" / "src" / "pbip_api"
CODE_ROOTS = [ROOT, _API, _API / "core", _API / "modules"]

_TEST_REF = re.compile(r"`(test_[A-Za-z0-9_]+?)(?:\.py|\.\.\.)?`")
_TEST_FILE_REF = re.compile(r"`(test_[A-Za-z0-9_]+\.py)`")
_CODE_REF = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|tsx|ts))(?::\s*([A-Za-z0-9_.]+))?[^`]*`")


def _text() -> str:
    return THREAT_MODEL.read_text(encoding="utf-8")


def _test_sources() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for base in TEST_DIRS for path in base.rglob("test_*.py")
    )


def test_cited_test_files_exist() -> None:
    names = {path.name for base in TEST_DIRS for path in base.rglob("test_*.py")}
    missing = sorted(set(_TEST_FILE_REF.findall(_text())) - names)
    assert not missing, f"threat model cites missing test files: {missing}"


def test_cited_test_functions_exist() -> None:
    sources = _test_sources()
    file_stems = {path.stem for base in TEST_DIRS for path in base.rglob("test_*.py")}
    cited = set(_TEST_REF.findall(_text())) - file_stems
    # A trailing "..." in the document abbreviates a name, so a cited name may be a prefix.
    missing = sorted(name for name in cited if f"def {name}" not in sources)
    assert cited, "expected the threat model to cite tests as evidence"
    assert not missing, f"threat model cites missing test functions: {missing}"


def test_cited_code_paths_and_symbols_exist() -> None:
    problems: list[str] = []
    for path_text, symbol in _CODE_REF.findall(_text()):
        if path_text.split("/")[-1].startswith("test_"):
            continue  # covered by the test-file check
        matches = [root / path_text for root in CODE_ROOTS if (root / path_text).is_file()]
        if not matches:
            problems.append(f"missing file {path_text}")
            continue
        name = symbol.split(".")[-1] if symbol else ""
        if name and not re.search(
            rf"\b{re.escape(name)}\b", matches[0].read_text(encoding="utf-8")
        ):
            problems.append(f"{path_text} has no {name}")
    assert not problems, f"threat model cites code that does not exist: {problems}"


def test_code_reference_keeps_the_full_tsx_extension() -> None:
    # Alternation order matters: `ts` before `tsx` captured "x.test.ts" and reported a .tsx
    # citation as a missing file.
    match = _CODE_REF.search("`apps/web/src/shell/routing.test.tsx`")
    assert match is not None
    assert match.group(1) == "apps/web/src/shell/routing.test.tsx"
