"""Unit tests for scripts/init_env.py (.env creation during `setup`).

Verifies:
- The script imports only the standard library: `setup` runs it on a fresh clone before any
  dependency is installed (native Windows run: ModuleNotFoundError: cryptography, #24).
- The generated FIELD_ENCRYPTION_KEY is a valid Fernet key.
- Secrets are generated and the database URLs follow the generated Postgres password.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

import scripts.init_env as init_env

pytestmark = pytest.mark.unit

EXAMPLE = (
    "JWT_SECRET=change_me\n"
    "FIELD_ENCRYPTION_KEY=change_me\n"
    "POSTGRES_PASSWORD=pbip_local_password\n"  # pragma: allowlist secret
    "GRAFANA_ADMIN_PASSWORD=change_me\n"
    "DATABASE_URL=postgresql+psycopg://pbip:pbip_local_password@localhost:5432/pbip\n"  # pragma: allowlist secret
)


def test_init_env_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(init_env.__file__).read_text(encoding="utf-8"))
    modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert modules <= sys.stdlib_module_names, modules - sys.stdlib_module_names


def test_fernet_key_is_accepted_by_cryptography() -> None:
    key = init_env.fernet_key()
    token = Fernet(key.encode()).encrypt(b"pbip")
    assert Fernet(key.encode()).decrypt(token) == b"pbip"
    assert key != init_env.fernet_key()


def test_main_writes_fresh_secrets(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    with patch.object(init_env, "ROOT", tmp_path), patch("sys.argv", ["init_env.py"]):
        assert init_env.main() == 0
    values = dict(
        line.split("=", 1) for line in (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    )
    assert "change_me" not in values.values()
    assert "pbip_local_password" not in "".join(values.values())
    Fernet(values["FIELD_ENCRYPTION_KEY"].encode())
    assert f":{values['POSTGRES_PASSWORD']}@" in values["DATABASE_URL"]


def test_main_keeps_an_existing_env(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    (tmp_path / ".env").write_text("KEEP=1\n", encoding="utf-8")
    with patch.object(init_env, "ROOT", tmp_path), patch("sys.argv", ["init_env.py"]):
        assert init_env.main() == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "KEEP=1\n"
