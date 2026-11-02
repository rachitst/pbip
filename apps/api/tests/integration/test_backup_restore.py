"""Backup -> encrypted file -> restore drill against real PostgreSQL (runbook section 4, TASK-083).

Dumps the migrated test database with real data in it, restores into a fresh
``pbip_restore_drill_*`` database on the same server and checks the drill both passes on an intact
backup and fails (and still cleans up) when the manifest disagrees with what was restored.

Needs ``pg_dump``/``pg_restore`` and ``gpg`` on the host, or set ``PBIP_TEST_PG_CONTAINER`` to the
name of the PostgreSQL container serving ``TEST_DATABASE_URL`` to run the libpq tools inside it.
"""

import json
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pbip_api.modules.compliance.service import AuditService
from pbip_test_support import Actor
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import scripts.backup as backup
import scripts.restore as restore

pytestmark = pytest.mark.integration

RESTORE_BACKUP = sys.modules[restore.Manifest.__module__]


@pytest.fixture
def gpg_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """A throwaway keyring so the test never touches the developer's own gpg state."""
    home = tmp_path_factory.mktemp("gpg")
    home.chmod(0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    yield home
    # Stop the gpg-agent this home started. Fixed argv, no user input.
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=False)  # noqa: S603, S607


@pytest.fixture
def container() -> str | None:
    name = os.environ.get("PBIP_TEST_PG_CONTAINER") or None
    missing = backup.PgTools(name).missing_executables("pg_dump")
    missing += backup.PgTools(name).missing_executables("pg_restore")
    if missing:
        reason = f"backup drill needs {sorted(set(missing))} on PATH"
        if os.environ.get("CI"):
            pytest.fail(reason)
        pytest.skip(f"{reason} (or set PBIP_TEST_PG_CONTAINER)")
    return name


@pytest.fixture
def passphrase(tmp_path: Path) -> Path:
    path = tmp_path / "passphrase"
    path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    path.chmod(0o600)
    return path


def _drill_databases(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT datname FROM pg_database WHERE datname LIKE 'pbip_restore_drill_%'")
        ).scalars()
        return list(rows)


def _seed(make_customer: Callable[..., Actor], engine: Engine) -> None:
    make_customer(funds=250_000)
    make_customer(funds=1_000)
    with sessionmaker(bind=engine)() as session:
        AuditService(session).record(None, "BACKUP_DRILL_TEST", "test", "drill")
        session.commit()


def _backup(url: str, out_dir: Path, passphrase: Path, container: str | None) -> Path:
    plan = backup.BackupPlan(
        url=make_url(url),
        out_dir=out_dir,
        encryption=backup.Encryption(passphrase_file=passphrase),
        tools=backup.PgTools(container),
        now=datetime.now(UTC),
    )
    manifest = backup.run_backup(plan)
    assert manifest.stats.ledger.customer_balance_total == 251_000
    assert manifest.stats.audit.rows >= 1
    assert backup.integrity_problems(manifest.stats) == []
    assert plan.dump_path.stat().st_mode & 0o777 == 0o600
    assert b"PGDMP" not in plan.dump_path.read_bytes()[:64]  # encrypted, not a raw dump
    return plan.dump_path


def _restore_plan(
    dump: Path, url: str, passphrase: Path, container: str | None
) -> restore.RestorePlan:
    return restore.RestorePlan(
        dump=dump,
        url=make_url(url),
        encryption=restore.Encryption(passphrase_file=passphrase),
        tools=restore.PgTools(container),
        database=restore.drill_database_name(datetime.now(UTC)),
    )


def test_backup_restores_completely_and_the_drill_cleans_up(
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    test_database_url: str,
    tmp_path: Path,
    gpg_home: Path,
    passphrase: Path,
    container: str | None,
) -> None:
    _seed(make_customer, migrated_engine)
    dump = _backup(test_database_url, tmp_path, passphrase, container)
    manifest = RESTORE_BACKUP.Manifest.from_json(
        restore.manifest_path_for(dump).read_text(encoding="utf-8")
    )

    result = restore.run_drill(_restore_plan(dump, test_database_url, passphrase, container))

    assert result.ok, result.problems
    assert result.restored is not None
    assert result.restored.row_counts == manifest.stats.row_counts
    assert result.restored.row_counts["public.transaction_entries"] == 4  # two deposits
    assert result.restored.ledger == manifest.stats.ledger
    assert result.restored.audit.chain_valid
    assert result.restored.audit.head_hash == manifest.stats.audit.head_hash
    assert result.database not in _drill_databases(migrated_engine)


def test_drill_fails_when_the_restore_does_not_match_the_manifest(
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    test_database_url: str,
    tmp_path: Path,
    gpg_home: Path,
    passphrase: Path,
    container: str | None,
) -> None:
    _seed(make_customer, migrated_engine)
    dump = _backup(test_database_url, tmp_path, passphrase, container)
    manifest_path = restore.manifest_path_for(dump)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["stats"]["row_counts"]["public.audit_logs"] += 1  # the backup "had" one more row
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    result = restore.run_drill(_restore_plan(dump, test_database_url, passphrase, container))

    assert not result.ok
    assert any(p.startswith("row count public.audit_logs") for p in result.problems)
    assert result.database not in _drill_databases(migrated_engine)
