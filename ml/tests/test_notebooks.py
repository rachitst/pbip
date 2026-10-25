"""TASK-010 definition of done: the EDA notebook executes head-to-tail without errors."""

from pathlib import Path

import pytest

from scripts.run_notebooks import NOTEBOOK_DIR, execute

pytestmark = [pytest.mark.unit, pytest.mark.slow]

NOTEBOOKS = sorted(NOTEBOOK_DIR.glob("*.ipynb"))


def _stdout(notebook: object) -> str:
    return "".join(
        output.get("text", "")
        for cell in notebook.cells  # type: ignore[attr-defined]  # nbformat NotebookNode
        for output in cell.get("outputs", [])
        if output.get("output_type") == "stream" and output.get("name") == "stdout"
    )


def test_there_is_an_eda_notebook() -> None:
    assert [p.name for p in NOTEBOOKS] == ["01_synthetic_eda.ipynb"]


@pytest.mark.timeout(600)
@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: Path(p).stem)
def test_notebook_executes_head_to_tail(path: Path) -> None:
    notebook = execute(path)  # raises CellExecutionError on any failing cell
    errors = [
        o for c in notebook.cells for o in c.get("outputs", []) if o.get("output_type") == "error"
    ]
    assert errors == []
    assert "point-in-time invariance: OK; label fields rejected: OK" in _stdout(notebook)
