"""scripts/backup.py and scripts/restore.py without a database (runbook section 4, TASK-083).

The end-to-end dump -> encrypt -> decrypt -> restore -> verify round trip against PostgreSQL is in
tests/integration/test_backup_restore.py; these tests pin the pieces that decide whether a drill
passes or fails, and prove a dry run executes nothing.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.engine import make_url

import scripts.backup as backup
import scripts.restore as restore

pytestmark = pytest.mark.unit

# restore.py imports backup.py by file (it runs as a script), so it holds its own module object.
RESTORE_BACKUP = sys.modules[restore.Manifest.__module__]

NOW = datetime(2026, 11, 1, 14, 1, 23, tzinfo=UTC)
DB_PASSWORD = secrets.token_urlsafe(16)  # generated per run: no credential literal in tests
URL = make_url("postgresql+psycopg://pbip@db.example:5433/pbip").set(password=DB_PASSWORD)


def _stats(module: Any = backup, **overrides: Any) -> Any:
    """A healthy DatabaseStats from ``module`` (restore.py binds its own copy of backup.py)."""
    ledger = module.LedgerStats(
        accounts=3,
        customer_balance_total=250_000,
        held_total=10_000,
        net_balance=0,
        balances_sha256="b" * 64,
        unreconciled_accounts=0,
        unbalanced_transactions=0,
    )
    audit = module.AuditStats(
        rows=4,
        head_seq=4,
        head_hash="a" * 64,
        chain_valid=True,
        chain_checked=4,
        first_invalid_seq=None,
    )
    stats = module.DatabaseStats(
        alembic_revision="0009",
        row_counts={"public.accounts": 3, "public.audit_logs": 4, "public.transactions": 2},
        ledger=ledger,
        audit=audit,
    )
    return replace(stats, **overrides)


def _manifest(module: Any, dump: Path, stats: Any) -> Any:
    data = dump.read_bytes()
    return module.Manifest(
        created_at=NOW.isoformat(),
        database="pbip",
        dump_file=dump.name,
        dump_sha256=hashlib.sha256(data).hexdigest(),
        dump_bytes=len(data),
        encryption="symmetric-aes256",
        server_version="16.15",
        stats=stats,
    )


@pytest.fixture
def passphrase_file(tmp_path: Path) -> Path:
    path = tmp_path / "passphrase"
    path.write_text("generated-for-this-test-only\n", encoding="utf-8")
    path.chmod(0o600)
    return path


# --------------------------------------------------------------------------- names and manifest


def test_backup_stem_is_a_utc_timestamp() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    assert backup.backup_stem("pbip", NOW) == "pbip-pbip-20261101T140123Z"
    assert backup.backup_stem("pbip", NOW.astimezone(ist)) == "pbip-pbip-20261101T140123Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        backup.backup_stem("pbip", NOW.replace(tzinfo=None))


def test_manifest_sits_next_to_the_dump() -> None:
    dump = Path("/b/pbip-pbip-20261101T140123Z.dump.gpg")
    assert backup.manifest_path_for(dump) == Path("/b/pbip-pbip-20261101T140123Z.manifest.json")
    with pytest.raises(backup.BackupError, match=r"\.dump\.gpg"):
        backup.manifest_path_for(Path("/b/pbip.sql"))


def test_drill_database_name_is_unique_and_namespaced() -> None:
    name = restore.drill_database_name(NOW, "abc123")
    assert name == "pbip_restore_drill_20261101t140123z_abc123"
    assert restore.drill_database_name(NOW) != restore.drill_database_name(NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        restore.drill_database_name(NOW.replace(tzinfo=None))
    with pytest.raises(restore.BackupError):
        restore.drill_database_name(NOW, 'x"; DROP DATABASE pbip; --')


def test_manifest_round_trips_through_json(tmp_path: Path) -> None:
    dump = tmp_path / "pbip-pbip-20261101T140123Z.dump.gpg"
    dump.write_bytes(b"ciphertext")
    manifest = _manifest(backup, dump, _stats())
    assert backup.Manifest.from_json(manifest.to_json()) == manifest
    assert json.loads(manifest.to_json())["version"] == backup.MANIFEST_VERSION


@pytest.mark.parametrize(
    "raw",
    ["{}", "[]", "not json", json.dumps({"version": 99}), json.dumps({"version": 1, "x": 1})],
)
def test_malformed_manifest_is_rejected(raw: str) -> None:
    with pytest.raises(backup.BackupError):
        backup.Manifest.from_json(raw)


# --------------------------------------------------------------------------- comparison


def test_identical_stats_compare_clean() -> None:
    assert restore.compare_stats(_stats(RESTORE_BACKUP), _stats(RESTORE_BACKUP)) == []


def test_row_count_difference_is_reported_per_table() -> None:
    expected = _stats(RESTORE_BACKUP)
    actual = replace(expected, row_counts={**expected.row_counts, "public.transactions": 1})
    assert restore.compare_stats(expected, actual) == [
        "row count public.transactions: restored 1 != backup 2"
    ]


def test_missing_and_unexpected_tables_are_reported() -> None:
    expected = _stats(RESTORE_BACKUP)
    counts = {k: v for k, v in expected.row_counts.items() if k != "public.audit_logs"}
    actual = replace(expected, row_counts={**counts, "public.extra": 0})
    assert restore.compare_stats(expected, actual) == [
        "table missing after restore: public.audit_logs",
        "unexpected table after restore: public.extra",
    ]


def test_ledger_audit_head_and_revision_differences_are_reported() -> None:
    expected = _stats(RESTORE_BACKUP)
    actual = replace(
        expected,
        alembic_revision="0008",
        ledger=replace(expected.ledger, customer_balance_total=249_999),
        audit=replace(expected.audit, head_hash="c" * 64),
    )
    problems = restore.compare_stats(expected, actual)
    assert len(problems) == 3
    assert problems[0].startswith("alembic revision")
    assert "ledger.customer_balance_total: restored 249999 != backup 250000" in problems
    assert any(p.startswith("audit.head_hash") for p in problems)


def test_healthy_stats_have_no_integrity_problems() -> None:
    assert backup.integrity_problems(_stats()) == []


@pytest.mark.parametrize(
    ("ledger", "audit", "expected"),
    [
        ({}, {"chain_valid": False, "first_invalid_seq": 3}, "chain broken at seq 3"),
        ({}, {"chain_checked": 3}, "verified 3 rows of 4"),
        ({"unreconciled_accounts": 1}, {}, "1 accounts do not match"),
        ({"unbalanced_transactions": 2}, {}, "2 transactions do not balance"),
        ({"net_balance": 5}, {}, "net balance is 5"),
    ],
)
def test_integrity_problems_are_detected(
    ledger: dict[str, Any], audit: dict[str, Any], expected: str
) -> None:
    healthy = _stats()
    broken = replace(
        healthy, ledger=replace(healthy.ledger, **ledger), audit=replace(healthy.audit, **audit)
    )
    problems = backup.integrity_problems(broken)
    assert len(problems) == 1
    assert expected in problems[0]


# --------------------------------------------------------------------------- commands and secrets


def test_symmetric_encryption_reads_the_passphrase_from_a_file(passphrase_file: Path) -> None:
    encryption = backup.Encryption(passphrase_file=passphrase_file)
    command = encryption.encrypt_command()
    assert command[:2] == ["gpg", "--batch"]
    assert ["--passphrase-file", str(passphrase_file)] == command[
        command.index("--passphrase-file") : command.index("--passphrase-file") + 2
    ]
    assert command[-3:] == ["--symmetric", "--cipher-algo", "AES256"]
    assert "--no-symkey-cache" in command
    secret = passphrase_file.read_text(encoding="utf-8").strip()
    assert not any(secret in part for part in command)
    assert encryption.decrypt_command(Path("x.dump.gpg"))[-2:] == ["--decrypt", "x.dump.gpg"]


def test_public_key_encryption_and_invalid_combinations(passphrase_file: Path) -> None:
    recipient = backup.Encryption(recipient="ops@bank.example")
    assert recipient.encrypt_command()[-3:] == ["--encrypt", "--recipient", "ops@bank.example"]
    assert recipient.mode == "public-key"
    with pytest.raises(backup.BackupError):
        backup.Encryption(passphrase_file=passphrase_file, recipient="ops@bank.example")
    with pytest.raises(backup.BackupError):
        backup.Encryption().encrypt_command()
    assert "--passphrase-file" not in backup.Encryption().decrypt_command(Path("x"))


def test_passphrase_file_must_be_private(tmp_path: Path, passphrase_file: Path) -> None:
    backup.check_passphrase_file(passphrase_file)
    passphrase_file.chmod(0o644)
    with pytest.raises(backup.BackupError, match="chmod 600"):
        backup.check_passphrase_file(passphrase_file)
    with pytest.raises(backup.BackupError, match="not readable"):
        backup.check_passphrase_file(tmp_path / "missing")


def test_tools_run_on_host_or_in_container_without_the_password_in_argv() -> None:
    target = backup.PgTarget.from_url(URL)
    assert DB_PASSWORD not in repr(target)
    host = backup.PgTools().command("pg_dump", target, "pbip", ["--format=custom"])
    assert host == [
        "pg_dump",
        "-h",
        "db.example",
        "-p",
        "5433",
        "-U",
        "pbip",
        "-d",
        "pbip",
        "--no-password",
        "--format=custom",
    ]
    docker = backup.PgTools("pbip-db-1").command("pg_restore", target, "drill", [])
    assert docker[:7] == ["docker", "exec", "-i", "-e", "PGPASSWORD", "pbip-db-1", "pg_restore"]
    for argv in (host, docker):
        assert not any(DB_PASSWORD in part for part in argv)
    assert backup.PgTools().env(target)["PGPASSWORD"] == DB_PASSWORD
    with pytest.raises(backup.BackupError):
        backup.PgTools("db; rm -rf /")


def test_run_pipeline_reports_both_exit_codes(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with out.open("wb") as handle:
        codes = backup.run_pipeline(["printf", "abc"], ["cat"], {"PATH": "/usr/bin:/bin"}, handle)
    assert codes == (0, 0)
    assert out.read_bytes() == b"abc"
    assert backup.run_pipeline(["false"], ["cat"], {"PATH": "/usr/bin:/bin"}) == (1, 0)


def test_a_failing_pipeline_stage_fails_the_dump(tmp_path: Path) -> None:
    plan = backup.BackupPlan(
        URL, tmp_path, backup.Encryption(recipient="ops@bank.example"), backup.PgTools(), NOW
    )
    partial = tmp_path / "x.partial"
    with (
        patch.object(backup, "run_pipeline", return_value=(1, 0)),
        pytest.raises(backup.BackupError, match="pg_dump exited 1, gpg exited 0"),
    ):
        backup._dump_into(plan, "00000003-1", partial)


# --------------------------------------------------------------------------- dry runs


def _no_execution() -> Any:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a dry run must not execute anything")

    return refuse


def test_backup_dry_run_prints_the_plan_and_executes_nothing(
    tmp_path: Path, passphrase_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = SimpleNamespace(database_url=URL.render_as_string(hide_password=False))
    with (
        patch.object(backup, "get_settings", return_value=settings),
        patch.object(backup, "create_engine", _no_execution()),
        patch.object(backup.subprocess, "Popen", _no_execution()),
    ):
        code = backup.main(
            [
                "backup.py",
                "--out-dir",
                str(tmp_path),
                "--passphrase-file",
                str(passphrase_file),
                "--docker-container",
                "pbip-db-1",
                "--dry-run",
            ]
        )
    out = capsys.readouterr().out
    assert code == 0
    assert "dry run: nothing was executed" in out
    assert "pg_export_snapshot" in out
    assert "docker exec -i -e PGPASSWORD pbip-db-1 pg_dump" in out
    assert DB_PASSWORD not in out
    assert list(tmp_path.iterdir()) == [passphrase_file]


def test_backup_refuses_a_world_readable_passphrase_file(
    tmp_path: Path, passphrase_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    passphrase_file.chmod(0o644)
    code = backup.main(
        ["backup.py", "--out-dir", str(tmp_path), "--passphrase-file", str(passphrase_file)]
    )
    assert code == 1
    assert "chmod 600" in capsys.readouterr().err


def test_restore_dry_run_prints_the_plan_and_executes_nothing(
    tmp_path: Path, passphrase_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = SimpleNamespace(database_url=URL.render_as_string(hide_password=False))
    dump = tmp_path / "pbip-pbip-20261101T140123Z.dump.gpg"
    with (
        patch.object(restore, "get_settings", return_value=settings),
        patch.object(restore, "create_engine", _no_execution()),
        patch.object(restore, "run_pipeline", _no_execution()),
    ):
        code = restore.main(
            ["restore.py", str(dump), "--passphrase-file", str(passphrase_file), "--dry-run"]
        )
    out = capsys.readouterr().out
    assert code == 0
    assert "CREATE DATABASE pbip_restore_drill_" in out
    assert "pg_restore -h db.example -p 5433" in out
    assert "DROP DATABASE pbip_restore_drill_" in out
    assert DB_PASSWORD not in out


# --------------------------------------------------------------------------- drill outcomes


class _FakeServer:
    """Stands in for CREATE/DROP DATABASE, pg_restore and the restored copy's statistics."""

    def __init__(self, restored: Any, restore_error: Exception | None = None) -> None:
        self.restored = restored
        self.restore_error = restore_error
        self.created: list[str] = []
        self.dropped: list[str] = []

    def patches(self) -> Any:
        def restore_into(_plan: Any) -> None:
            if self.restore_error is not None:
                raise self.restore_error

        return (
            patch.object(restore, "create_database", lambda _url, name: self.created.append(name)),
            patch.object(restore, "drop_database", lambda _url, name: self.dropped.append(name)),
            patch.object(restore, "restore_into", restore_into),
            patch.object(restore, "restored_stats", lambda _url, _db: self.restored),
        )


def _drill(tmp_path: Path, manifest_stats: Any, server: _FakeServer, **plan: Any) -> Any:
    dump = tmp_path / "pbip-pbip-20261101T140123Z.dump.gpg"
    dump.write_bytes(b"pretend ciphertext")
    manifest = _manifest(restore, dump, manifest_stats)
    restore.manifest_path_for(dump).write_text(manifest.to_json(), encoding="utf-8")
    restore_plan = restore.RestorePlan(
        dump=dump,
        url=URL,
        encryption=restore.Encryption(recipient=None),
        tools=restore.PgTools(),
        database=restore.drill_database_name(NOW, "t1"),
        **plan,
    )
    first, second, third, fourth = server.patches()
    with first, second, third, fourth:
        return restore.run_drill(restore_plan), dump


def test_drill_passes_when_the_copy_matches_and_drops_the_database(tmp_path: Path) -> None:
    server = _FakeServer(_stats(RESTORE_BACKUP))
    result, _ = _drill(tmp_path, _stats(RESTORE_BACKUP), server)
    assert result.ok
    assert server.created == server.dropped == ["pbip_restore_drill_20261101t140123z_t1"]
    assert restore.report(result)["result"] == "PASS"


def test_drill_fails_on_a_checksum_mismatch_before_touching_the_server(tmp_path: Path) -> None:
    server = _FakeServer(_stats(RESTORE_BACKUP))
    dump = tmp_path / "pbip-pbip-20261101T140123Z.dump.gpg"
    dump.write_bytes(b"pretend ciphertext")
    manifest = _manifest(restore, dump, _stats(RESTORE_BACKUP))
    dump.write_bytes(b"pretend ciphertexT")  # same size, one byte flipped
    restore.manifest_path_for(dump).write_text(manifest.to_json(), encoding="utf-8")
    plan = restore.RestorePlan(
        dump, URL, restore.Encryption(), restore.PgTools(), restore.drill_database_name(NOW, "t2")
    )
    first, second, third, fourth = server.patches()
    with first, second, third, fourth:
        result = restore.run_drill(plan)
    assert result.problems == ["checksum: sha256 does not match the manifest"]
    assert server.created == []


def test_drill_fails_on_a_row_count_mismatch_and_still_drops(tmp_path: Path) -> None:
    expected = _stats(RESTORE_BACKUP)
    short = replace(expected, row_counts={**expected.row_counts, "public.audit_logs": 3})
    server = _FakeServer(short)
    result, _ = _drill(tmp_path, expected, server)
    assert not result.ok
    assert result.problems == ["row count public.audit_logs: restored 3 != backup 4"]
    assert server.dropped == server.created
    assert restore.report(result)["result"] == "FAIL"


def test_drill_fails_when_the_restored_chain_is_broken(tmp_path: Path) -> None:
    expected = _stats(RESTORE_BACKUP)
    broken = replace(
        expected, audit=replace(expected.audit, chain_valid=False, first_invalid_seq=2)
    )
    result, _ = _drill(tmp_path, expected, _FakeServer(broken))
    assert result.problems == ["audit hash chain broken at seq 2"]


def test_drill_records_a_failed_restore_and_keeps_on_request(tmp_path: Path) -> None:
    server = _FakeServer(None, restore.BackupError("gpg exited 2, pg_restore exited 1"))
    result, _ = _drill(tmp_path, _stats(RESTORE_BACKUP), server, keep=True)
    assert result.problems == ["gpg exited 2, pg_restore exited 1"]
    assert server.created and server.dropped == []  # --keep leaves it for investigation


def test_restore_main_exits_non_zero_on_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = restore.DrillResult("pbip_restore_drill_x", ["row count x: 1 != 2"], {})
    dump = tmp_path / "pbip-pbip-20261101T140123Z.dump.gpg"
    dump.write_bytes(b"c")
    restore.manifest_path_for(dump).write_text("{}", encoding="utf-8")
    settings = SimpleNamespace(database_url=URL.render_as_string(hide_password=False))
    with (
        patch.object(restore, "get_settings", return_value=settings),
        patch.object(restore.PgTools, "missing_executables", lambda _self, _tool: []),
        patch.object(restore, "run_drill", return_value=failed),
    ):
        code = restore.main(["restore.py", str(dump)])
    assert code == 1
    assert '"result": "FAIL"' in capsys.readouterr().out
