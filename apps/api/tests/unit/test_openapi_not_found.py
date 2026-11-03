"""#175: every operation with a path parameter documents its 404 in the committed contract."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[4] / "docs" / "api" / "openapi.json"


def test_every_operation_with_a_path_parameter_documents_404() -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    missing = [
        f"{method.upper()} {path}"
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if any(p["in"] == "path" for p in operation.get("parameters", []))
        and "404" not in operation["responses"]
    ]
    assert missing == []
