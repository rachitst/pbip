"""Check that every relative link and image in the repository's Markdown files resolves.

Scans every Markdown file tracked by git (or the files named on the command line) for inline
links and images (``[text](target)``, ``![alt](target)``), reference definitions
(``[id]: target``) and HTML ``href``/``src`` attributes. A relative target must name an existing
file or directory; a ``#fragment`` on a Markdown target must match one of its headings (GitHub's
anchor rules) or an explicit ``id``/``name`` anchor. External links (``http``, ``https``,
``mailto``) are not fetched, so CI never depends on other people's servers. Code blocks and
inline code are ignored.

Usage: ``python scripts/check_links.py [file.md ...]``. Exit code 0 when every link resolves,
1 otherwise. Standard library only, so it runs before any dependency is installed.
"""

import re
import subprocess
import sys
import unicodedata
from functools import cache
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_INLINE_LINK = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)"
)
_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s+<?(\S+?)>?(?:\s+.*)?$")
_HTML_ATTR = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HTML_ANCHOR = re.compile(
    r"""<a\s+(?:[^>]*?\s)?(?:id|name)\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)


def _outside_fences(lines: list[str]) -> list[str]:
    """The lines with fenced code blocks blanked, so line numbers stay right."""
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        match = _FENCE.match(line)
        if fence is not None:
            if match and match.group(1) == fence:
                fence = None
            out.append("")
            continue
        if match:
            fence = match.group(1)
            out.append("")
            continue
        out.append(line)
    return out


def github_slug(heading: str) -> str:
    """GitHub's heading anchor: markup removed, lower case, punctuation dropped, spaces to -."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links keep their text
    text = re.sub(r"<[^>]+>", "", text).replace("`", "")
    text = re.sub(r"(\*\*|__|\*|_(?=\W)|(?<=\W)_)", "", text)
    kept = []
    for char in text.strip().lower():
        category = unicodedata.category(char)
        if char in " -_" or category[0] in "LN" or category == "Mn":
            kept.append(char)
    return "".join(kept).replace(" ", "-")


@cache
def anchors(path: Path) -> frozenset[str]:
    """Every anchor a Markdown file defines: headings (GitHub's -1, -2 suffixes) and HTML ids."""
    found: set[str] = set()
    counts: dict[str, int] = {}
    for line in _outside_fences(path.read_text(encoding="utf-8").splitlines()):
        found.update(m.group(1) for m in _HTML_ANCHOR.finditer(line))
        match = _HEADING.match(line)
        if match:
            slug = github_slug(match.group(2))
            n = counts.get(slug, 0)
            counts[slug] = n + 1
            found.add(slug if n == 0 else f"{slug}-{n}")
    return frozenset(found)


def _targets(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    result: list[tuple[int, str]] = []
    for number, text in enumerate(_outside_fences(lines), start=1):
        line = _INLINE_CODE.sub("", text)
        result.extend((number, m.group(1)) for m in _INLINE_LINK.finditer(line))
        result.extend((number, m.group(1)) for m in _HTML_ATTR.finditer(line))
        reference = _REFERENCE.match(line)
        if reference:
            result.append((number, reference.group(1)))
    return result


def problem(source: Path, target: str) -> str | None:
    """Why ``target`` (as written in ``source``) does not resolve, or None if it does."""
    if _EXTERNAL.match(target):
        return None
    link, _, fragment = target.partition("#")
    link = unquote(link.split("?", 1)[0])
    if link.startswith("/"):
        resolved = ROOT / link.lstrip("/")
    else:
        resolved = (source.parent / link) if link else source
    resolved = resolved.resolve()
    if not resolved.is_relative_to(ROOT):
        return "points outside the repository"
    if not resolved.exists():
        return "file not found"
    is_markdown = resolved.is_file() and resolved.suffix.lower() == ".md"
    if fragment and is_markdown and unquote(fragment).lower() not in anchors(resolved):
        where = resolved.relative_to(ROOT).as_posix()
        return f"no heading or anchor '#{fragment}' in {where}"
    return None


def markdown_files(arguments: list[str]) -> list[Path]:
    if arguments:
        return [Path(a).resolve() for a in arguments]
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return sorted(ROOT / name for name in listed.split("\0") if name)


def main(arguments: list[str]) -> int:
    files = markdown_files(arguments)
    broken = 0
    for path in files:
        for number, target in _targets(path):
            reason = problem(path, target)
            if reason is not None:
                broken += 1
                print(f"{path.relative_to(ROOT).as_posix()}:{number}: {target}: {reason}")
    if broken:
        print(f"{broken} broken link(s) in {len(files)} Markdown file(s)")
        return 1
    print(f"All relative links and images resolve in {len(files)} Markdown file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
