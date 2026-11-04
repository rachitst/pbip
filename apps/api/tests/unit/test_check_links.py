"""scripts/check_links.py: every relative Markdown link and image in the repository resolves.

The checker runs in CI on the whole repository; these tests pin what it reports on small files
written per test, and that the repository itself is clean.
"""

from pathlib import Path

import pytest

import scripts.check_links as links

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(links, "ROOT", tmp_path.resolve())
    links.anchors.cache_clear()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\n## 4.1 Transfer lifecycle (`transactions.status`)\n\n## Steps\n\n## Steps\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "img.png").write_bytes(b"\x89PNG")
    return tmp_path.resolve()


def _report(repo: Path, body: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    page = repo / "page.md"
    page.write_text(body, encoding="utf-8")
    code = links.main([str(page)])
    return code, capsys.readouterr().out


def test_resolving_links_images_and_anchors_pass(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        '[guide](docs/guide.md) ![img](docs/img.png) <img src="docs/img.png">\n'
        "[anchor](docs/guide.md#41-transfer-lifecycle-transactionsstatus)\n"
        "[second heading](docs/guide.md#steps-1) [folder](docs/) [web](https://example.org/x)\n"
        "[ref]: docs/guide.md\n"
    )
    code, out = _report(repo, body, capsys)
    assert code == 0, out


def test_missing_files_images_and_anchors_are_reported_with_their_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        "[gone](docs/missing.md)\n"
        "![img](docs/missing.png)\n"
        "[bad anchor](docs/guide.md#nope)\n"
        "[ref]: docs/also-missing.md\n"
        "[up](../outside.md)\n"
    )
    code, out = _report(repo, body, capsys)
    assert code == 1
    assert "page.md:1: docs/missing.md: file not found" in out
    assert "page.md:2: docs/missing.png: file not found" in out
    assert "page.md:3: docs/guide.md#nope: no heading or anchor '#nope'" in out
    assert "page.md:4: docs/also-missing.md: file not found" in out
    assert "page.md:5: ../outside.md: points outside the repository" in out
    assert "5 broken link(s)" in out


def test_code_is_not_checked(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    body = "`[inline](nope.md)`\n\n```\n[fenced](nope.md)\n```\n"
    code, out = _report(repo, body, capsys)
    assert code == 0, out


def test_the_repository_has_no_broken_links(capsys: pytest.CaptureFixture[str]) -> None:
    code = links.main([])
    assert code == 0, capsys.readouterr().out
