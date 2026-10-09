"""Regression: a process that imports only ONE module (Celery worker, scripts) must still be able
to resolve every cross-module foreign key. Run in a clean interpreter to prove it."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "module", ["pbip_api.modules.notifications.service", "pbip_api.modules.ledger.service"]
)
def test_importing_a_single_module_registers_all_tables(module: str) -> None:
    code = (
        f"import {module}; from pbip_api.core.db import Base; "
        "from sqlalchemy.orm import configure_mappers; configure_mappers(); "
        "Base.metadata.sorted_tables; "
        "assert 'payees' in Base.metadata.tables and 'users' in Base.metadata.tables"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
