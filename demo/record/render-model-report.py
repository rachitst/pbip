"""Render the fraud model's accuracy results as one page for the walkthrough video (item 14).

The page is cut from docs/ml/model-evaluation-report-v1.md, never retyped: the served model's four
promotion bars and confusion table (section 10), the components and the rules-only fail-safe (3.2),
and the precision-recall, cost and SHAP charts. Each excerpt is found by a marker line in the
report; a missing marker stops the script, so a reworded report cannot silently show stale numbers.

Usage (markdown-it-py is the only dependency, pinned in demo/record/README.md):
    python demo/record/render-model-report.py [OUT_DIR]   # default demo/record/out
It writes OUT_DIR/model-report/index.html and copies the three chart images next to it.
"""

from __future__ import annotations

import html
import shutil
import sys
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs/ml/model-evaluation-report-v1.md"

# (anchor id, heading on the page, first line marker, marker of the first line NOT included)
EXCERPTS: tuple[tuple[str, str, str, str], ...] = (
    (
        "four-bars",
        "The served model (hybrid-lgbm-iforest-3): the four promotion bars",
        "**The four bars (test / held-out),** measured",
        "On the test split the false holds are unchanged",
    ),
    (
        "components",
        "Each component, and the rules-only fail-safe (test split)",
        "| Scorer | Flagged at |",
        "![Precision-recall curves]",
    ),
    (
        "pr-curves",
        "Precision-recall curves",
        "![Precision-recall curves]",
        "- **LightGBM carries the hybrid.**",
    ),
    (
        "cost-curve",
        "Choosing the threshold by the cost of errors",
        "![Cost curve]",
        "- Missed fraud dominates",
    ),
    (
        "shap",
        "What drives the model: mean |SHAP| per feature",
        "![SHAP summary]",
        "**Tuning (TASK-084).**",
    ),
)

STYLE = """
:root { color-scheme: light; --ink: #0f172a; --muted: #475569; --line: #cbd5e1; --accent: #1d4ed8; }
* { box-sizing: border-box; }
body { margin: 0; background: #f8fafc; color: var(--ink);
  font: 18px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1500px; margin: 0 auto; padding: 32px 48px 160px; }
h1 { font-size: 40px; margin: 0 0 4px; }
.lede { color: var(--muted); margin: 0 0 28px; }
section { background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 24px 32px;
  margin: 0 0 28px; scroll-margin-top: 24px; }
h2 { font-size: 28px; margin: 0 0 12px; color: var(--accent); }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 18px; }
th, td { border: 1px solid var(--line); padding: 8px 12px; text-align: left; }
th { background: #eef2ff; }
img { display: block; max-width: 100%; max-height: 760px; margin: 8px auto; }
code { font-size: 0.92em; }
.note { color: var(--muted); font-size: 16px; }
"""


def excerpt(lines: list[str], start: str, stop: str) -> str:
    """The report lines from the one containing ``start`` up to (not including) ``stop``."""
    first = next((i for i, line in enumerate(lines) if start in line), None)
    if first is None:
        raise SystemExit(f"marker not found in the report: {start!r}")
    last = next((i for i in range(first + 1, len(lines)) if stop in lines[i]), None)
    if last is None:
        raise SystemExit(f"end marker not found after {start!r}: {stop!r}")
    return "\n".join(lines[first:last]).strip()


def render(report: str) -> str:
    md = MarkdownIt("commonmark").enable("table")
    lines = report.splitlines()
    sections = []
    for anchor, title, start, stop in EXCERPTS:
        body = md.render(excerpt(lines, start, stop))
        sections.append(f'<section id="{anchor}"><h2>{html.escape(title)}</h2>\n{body}</section>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>PBIP fraud model accuracy</title>"
        f"<style>{STYLE}</style></head><body><main>"
        "<h1>PBIP fraud model accuracy</h1>"
        '<p class="lede">From docs/ml/model-evaluation-report-v1.md. All data is synthetic '
        "(seeded generator): the numbers show the models separate the planted patterns; they "
        "are not an estimate on real bank traffic.</p>"
        + "\n".join(sections)
        + "</main></body></html>\n"
    )


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else ROOT / "demo/record/out"
    target = out / "model-report"
    (target / "img").mkdir(parents=True, exist_ok=True)
    for image in ("fraud-pr-curves.png", "fraud-cost-curve.png", "fraud-shap-summary.png"):
        shutil.copyfile(REPORT.parent / "img" / image, target / "img" / image)
    (target / "index.html").write_text(render(REPORT.read_text(encoding="utf-8")), encoding="utf-8")
    sys.stdout.write(f"{target / 'index.html'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
