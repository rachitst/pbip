#!/usr/bin/env python3
"""Execute the analysis notebooks head-to-tail (TASK-010 definition of done).

    uv run python scripts/run_notebooks.py           # re-execute and save outputs in place
    uv run python scripts/run_notebooks.py --check   # execute without writing; non-zero on error

Outputs are reproducible: notebooks generate their data from a fixed seed, figures are SVG with a
fixed hash salt, and per-cell execution timestamps are not recorded.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK_DIR = ROOT / "ml" / "notebooks"
TIMEOUT_SECONDS = 600


def execute(path: Path) -> nbformat.NotebookNode:
    notebook = nbformat.read(path, as_version=4)
    for cell in notebook.cells:
        cell.metadata.pop("execution", None)
    client = NotebookClient(
        notebook,
        timeout=TIMEOUT_SECONDS,
        kernel_name="python3",
        record_timing=False,
        resources={"metadata": {"path": str(path.parent)}},
    )
    client.execute()
    return notebook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="execute only, do not save outputs")
    parser.add_argument("notebooks", nargs="*", type=Path)
    args = parser.parse_args(argv)
    paths = args.notebooks or sorted(NOTEBOOK_DIR.glob("*.ipynb"))
    for path in paths:
        try:
            notebook = execute(path)
        except CellExecutionError as exc:
            sys.stderr.write(f"[FAIL] {path.name}: {exc.ename}: {exc.evalue}\n")
            return 1
        if not args.check:
            nbformat.write(notebook, path)
        sys.stdout.write(f"[OK] {path.name} executed head-to-tail\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
