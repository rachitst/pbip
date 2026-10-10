"""Unit tests for PBIP developer runner test environment loading and URL derivation.

Verifies:
- parse_env_file handles basic, quoted, escaped, and exported values.
- derive_test_database_url targets localhost:5432/${POSTGRES_DB}_test.
- Special URL characters in username and password are correctly encoded.
- build_test_env respects explicit TEST_DATABASE_URL overrides.
- build_test_env derives TEST_DATABASE_URL when absent.
- Explicit environment variables take precedence over .env file entries.
"""

from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from scripts.dev import (
    build_test_env,
    derive_test_database_url,
    parse_env_file,
)

pytestmark = pytest.mark.unit


def test_parse_env_file_missing_returns_empty(tmp_path: Path) -> None:
    non_existent = tmp_path / "does_not_exist.env"
    assert parse_env_file(non_existent) == {}


def test_parse_env_file_basic_and_comments(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
# A comment line
POSTGRES_USER=pbip
POSTGRES_DB=pbip # inline comment
export REDIS_URL=redis://localhost:6379/0
EMPTY_VAL=
    # indented comment
        KEY_WITH_SPACES   =   val_with_spaces
""",
        encoding="utf-8",
    )
    result = parse_env_file(env_file)
    assert result["POSTGRES_USER"] == "pbip"
    assert result["POSTGRES_DB"] == "pbip"
    assert result["REDIS_URL"] == "redis://localhost:6379/0"
    assert result["EMPTY_VAL"] == ""
    assert result["KEY_WITH_SPACES"] == "val_with_spaces"


def test_parse_env_file_quotes_and_escapes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        r"""
SINGLE_QUOTED='hello # not a comment'
DOUBLE_QUOTED="some \"value\" with \n and \t"
VAR_WITH_HASH="val#hash"
""",
        encoding="utf-8",
    )
    result = parse_env_file(env_file)
    assert result["SINGLE_QUOTED"] == "hello # not a comment"
    assert result["DOUBLE_QUOTED"] == 'some "value" with \n and \t'
    assert result["VAR_WITH_HASH"] == "val#hash"


def test_derive_test_database_url_default() -> None:
    url = derive_test_database_url()
    assert (
        url
        == "postgresql+psycopg://pbip:pbip_local_password@localhost:5432/pbip_test"  # pragma: allowlist secret
    )


def test_derive_test_database_url_custom_db() -> None:
    url = derive_test_database_url(
        user="custom_user",
        password="custom_password",  # pragma: allowlist secret
        db="retail_bank",
        host="localhost",
        port=5432,
    )
    assert (
        url
        == "postgresql+psycopg://custom_user:custom_password@localhost:5432/retail_bank_test"  # pragma: allowlist secret
    )


def test_derive_test_database_url_special_characters() -> None:
    special_user = "bank:user@domain"
    special_pass = "p@ss:w/ord#?%&+ !^$*"
    url = derive_test_database_url(
        user=special_user,
        password=special_pass,
        db="bank_db",
    )

    # Parse and verify URL scheme, host, port, path
    # Replace postgresql+psycopg with postgresql for standard urllib.parse compatibility
    parsed = urlparse(url.replace("postgresql+psycopg://", "postgresql://"))
    assert parsed.hostname == "localhost"
    assert parsed.port == 5432
    assert parsed.path == "/bank_db_test"

    # Verify unquoted credentials match original special characters exactly
    assert unquote(parsed.username or "") == special_user
    assert unquote(parsed.password or "") == special_pass

    # Ensure raw special characters are percent-encoded in the credentials portion
    credentials_part = url.split("://", 1)[1].split("@localhost", 1)[0]
    assert "@domain" not in credentials_part  # The @ was encoded
    assert "/" not in credentials_part  # Slash in password was encoded as %2F
    assert "%40" in credentials_part  # @ encoded
    assert "%3A" in credentials_part  # : encoded
    assert "%2F" in credentials_part  # / encoded
    assert "%23" in credentials_part  # # encoded
    assert "%3F" in credentials_part  # ? encoded
    assert "%25" in credentials_part  # % encoded
    assert "%26" in credentials_part  # & encoded


def test_build_test_env_respects_explicit_test_database_url(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TEST_DATABASE_URL=postgresql+psycopg://file_user:file_pass@localhost:5432/file_test\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    explicit_url = (
        "postgresql+psycopg://cli_user:cli_pass@remote:5432/cli_test"  # pragma: allowlist secret
    )
    result = build_test_env(
        env_path=env_file,
        explicit_env={"TEST_DATABASE_URL": explicit_url},
    )
    assert result["TEST_DATABASE_URL"] == explicit_url


def test_build_test_env_uses_file_test_database_url_if_not_in_explicit(
    tmp_path: Path,
) -> None:
    file_url = "postgresql+psycopg://file_user:file_pass@localhost:5432/file_test"  # pragma: allowlist secret
    env_file = tmp_path / ".env"
    env_file.write_text(f"TEST_DATABASE_URL={file_url}\n", encoding="utf-8")
    result = build_test_env(env_path=env_file, explicit_env={})
    assert result["TEST_DATABASE_URL"] == file_url


def test_build_test_env_derives_when_test_database_url_absent(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
POSTGRES_USER=admin_user
POSTGRES_PASSWORD=secret_pwd
POSTGRES_DB=core_banking
""",
        encoding="utf-8",
    )
    result = build_test_env(env_path=env_file, explicit_env={})
    assert (
        result["TEST_DATABASE_URL"]
        == "postgresql+psycopg://admin_user:secret_pwd@localhost:5432/core_banking_test"  # pragma: allowlist secret
    )


def test_build_test_env_explicit_overrides_file_variables(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_content = (
        "POSTGRES_USER=file_user\n"
        "POSTGRES_PASSWORD=file_pwd\n"
        "POSTGRES_DB=file_db\n"
        "DATABASE_URL=postgresql+psycopg://file_user:file_pwd@localhost:5432/file_db\n"  # pragma: allowlist secret
    )
    env_file.write_text(env_content, encoding="utf-8")
    # Explicit override of POSTGRES_USER should be used in derivation
    explicit = {"POSTGRES_USER": "override_user"}
    result = build_test_env(env_path=env_file, explicit_env=explicit)
    assert result["POSTGRES_USER"] == "override_user"
    assert result["POSTGRES_PASSWORD"] == "file_pwd"  # pragma: allowlist secret
    assert result["POSTGRES_DB"] == "file_db"
    assert (
        result["TEST_DATABASE_URL"]
        == "postgresql+psycopg://override_user:file_pwd@localhost:5432/file_db_test"  # pragma: allowlist secret
    )
    assert (
        result["DATABASE_URL"]
        == "postgresql+psycopg://file_user:file_pwd@localhost:5432/file_db"  # pragma: allowlist secret
    )


def test_build_test_env_derives_with_special_characters_in_env_file(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_content = (
        "POSTGRES_USER=bank_user\n"
        'POSTGRES_PASSWORD="pass/word@123#special"\n'  # pragma: allowlist secret
        "POSTGRES_DB=pbip\n"
    )
    env_file.write_text(env_content, encoding="utf-8")
    result = build_test_env(env_path=env_file, explicit_env={})
    assert (
        result["TEST_DATABASE_URL"]
        == "postgresql+psycopg://bank_user:pass%2Fword%40123%23special@localhost:5432/pbip_test"  # pragma: allowlist secret
    )
