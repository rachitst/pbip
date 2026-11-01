"""Restore drill: prove a backup from scripts/backup.py restores completely (runbook section 4.2).

    uv run python scripts/restore.py /var/backups/pbip/pbip-pbip-<stamp>.dump.gpg \\
        --passphrase-file FILE [--docker-container NAME] [--keep] [--dry-run]

Steps, each of which fails the drill (exit 1) on any mismatch:

1. the encrypted file's SHA-256 and size match the manifest (before anything is decrypted);
2. gpg decrypts it (gpg's integrity check fails on tampering) straight into ``pg_restore`` into a
   NEW temporary database ``pbip_restore_drill_<stamp>_<random>`` on the same server;
3. the restored copy has the same tables, per-table row counts, Alembic revision, ledger totals,
   per-account balance digest and audit-chain head as the snapshot the dump was taken from;
4. the restored copy is healthy on its own: the audit hash chain verifies from genesis with the
   project's ``AuditService.verify_chain`` (the code behind the compliance verify endpoint), every
   account balance equals its entries and every transaction balances.

The temporary database is dropped afterwards unless ``--keep`` is given. The live database is
never written: the only DDL this script issues is CREATE/DROP of the database it created.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup import (
    BackupError,
    DatabaseStats,
    Encryption,
    Manifest,
    PgTarget,
    PgTools,
    check_passphrase_file,
    collect_stats,
    integrity_problems,
    manifest_path_for,
    run_pipeline,
    sha256_file,
    shell_line,
)
from pbip_api.core.config import get_settings

DRILL_PREFIX = "pbip_restore_drill_"
_DRILL_NAME = re.compile(r"^pbip_restore_drill_[0-9a-z_]{1,40}$")


def drill_database_name(now: datetime, token: str | None = None) -> str:
    if now.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    suffix = token if token is not None else secrets.token_hex(3)
    name = f"{DRILL_PREFIX}{now.astimezone(UTC):%Y%m%dt%H%M%Sz}_{suffix}"
    if not _DRILL_NAME.match(name):
        raise BackupError("invalid drill database name")
    return name


def verify_checksum(dump: Path, manifest: Manifest) -> list[str]:
    problems: list[str] = []
    size = dump.stat().st_size
    if size != manifest.dump_bytes:
        problems.append(f"size {size} != manifest {manifest.dump_bytes}")
    if sha256_file(dump) != manifest.dump_sha256:
        problems.append("sha256 does not match the manifest")
    return problems


def _compare_counts(expected: dict[str, int], actual: dict[str, int]) -> list[str]:
    problems = [f"table missing after restore: {name}" for name in sorted(expected.keys() - actual)]
    problems += [
        f"unexpected table after restore: {name}" for name in sorted(actual.keys() - expected)
    ]
    problems += [
        f"row count {name}: restored {actual[name]} != backup {expected[name]}"
        for name in sorted(expected.keys() & actual.keys())
        if expected[name] != actual[name]
    ]
    return problems


def _compare_fields(section: str, expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    return [
        f"{section}.{key}: restored {actual[key]!r} != backup {expected[key]!r}"
        for key in sorted(expected)
        if expected[key] != actual.get(key)
    ]


def compare_stats(expected: DatabaseStats, actual: DatabaseStats) -> list[str]:
    """Every difference between the snapshot in the manifest and the restored copy."""
    problems: list[str] = []
    if expected.alembic_revision != actual.alembic_revision:
        problems.append(
            f"alembic revision: restored {actual.alembic_revision!r} "
            f"!= backup {expected.alembic_revision!r}"
        )
    problems += _compare_counts(expected.row_counts, actual.row_counts)
    problems += _compare_fields("ledger", asdict(expected.ledger), asdict(actual.ledger))
    audit_keys = ("rows", "head_seq", "head_hash")
    problems += _compare_fields(
        "audit",
        {key: getattr(expected.audit, key) for key in audit_keys},
        {key: getattr(actual.audit, key) for key in audit_keys},
    )
    return problems


@dataclass(frozen=True)
class RestorePlan:
    dump: Path
    url: URL
    encryption: Encryption
    tools: PgTools
    database: str
    keep: bool = False

    @property
    def manifest_path(self) -> Path:
        return manifest_path_for(self.dump)

    @property
    def target(self) -> PgTarget:
        return PgTarget.from_url(self.url)

    def restore_command(self) -> list[str]:
        args = ["--no-owner", "--exit-on-error"]
        return self.tools.command("pg_restore", self.target, self.database, args)

    def describe(self) -> list[str]:
        server = f"{self.target.host}:{self.target.port}"
        return [
            f"backup:   {self.dump}",
            f"manifest: {self.manifest_path}",
            "step 1:   sha256 + size of the encrypted file against the manifest",
            f"step 2:   CREATE DATABASE {self.database} on {server}",
            f"step 3:   {shell_line(self.encryption.decrypt_command(self.dump))}",
            f"          | {shell_line(self.restore_command())}",
            "step 4:   compare row counts, alembic revision, ledger totals and the audit-chain "
            "head with the manifest; AuditService.verify_chain(); ledger reconciliation",
            f"step 5:   {'keep' if self.keep else 'DROP DATABASE'} {self.database}",
        ]


@dataclass
class DrillResult:
    database: str
    problems: list[str]
    seconds: dict[str, float]
    restored: DatabaseStats | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def _admin_url(url: URL) -> URL:
    return url.set(database="postgres")


def create_database(url: URL, name: str) -> None:
    if not _DRILL_NAME.match(name):
        raise BackupError("refusing to create a database outside the drill namespace")
    engine = create_engine(_admin_url(url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    finally:
        engine.dispose()


def drop_database(url: URL, name: str) -> None:
    if not _DRILL_NAME.match(name):
        raise BackupError("refusing to drop a database outside the drill namespace")
    engine = create_engine(_admin_url(url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        engine.dispose()


def restore_into(plan: RestorePlan) -> None:
    gpg_code, restore_code = run_pipeline(
        plan.encryption.decrypt_command(plan.dump),
        plan.restore_command(),
        plan.tools.env(plan.target),
    )
    if gpg_code != 0 or restore_code != 0:
        raise BackupError(f"gpg exited {gpg_code}, pg_restore exited {restore_code}")


def restored_stats(url: URL, database: str) -> DatabaseStats:
    engine = create_engine(url.set(database=database), isolation_level="REPEATABLE READ")
    try:
        with engine.connect() as connection, connection.begin():
            return collect_stats(connection)
    finally:
        engine.dispose()


def _restore_and_verify(plan: RestorePlan, manifest: Manifest, result: DrillResult) -> None:
    started = time.monotonic()
    restore_into(plan)
    result.seconds["decrypt_and_restore"] = round(time.monotonic() - started, 1)
    started = time.monotonic()
    restored = restored_stats(plan.url, plan.database)
    result.seconds["verify"] = round(time.monotonic() - started, 1)
    result.restored = restored
    result.problems += compare_stats(manifest.stats, restored)
    result.problems += integrity_problems(restored)


def run_drill(plan: RestorePlan) -> DrillResult:
    """Verify, restore into a fresh database, compare, clean up. Never raises for a mismatch."""
    result = DrillResult(database=plan.database, problems=[], seconds={})
    manifest = Manifest.from_json(plan.manifest_path.read_text(encoding="utf-8"))
    result.problems += [f"checksum: {p}" for p in verify_checksum(plan.dump, manifest)]
    if result.problems:
        return result  # never decrypt or restore a file that is not the one we wrote
    create_database(plan.url, plan.database)
    try:
        _restore_and_verify(plan, manifest, result)
    except BackupError as exc:
        result.problems.append(str(exc))
    finally:
        if not plan.keep:
            drop_database(plan.url, plan.database)
    return result


def report(result: DrillResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "result": "PASS" if result.ok else "FAIL",
        "database": result.database,
        "seconds": result.seconds,
        "problems": result.problems,
    }
    if result.restored is not None:
        stats = result.restored
        out |= {
            "tables": len(stats.row_counts),
            "rows": sum(stats.row_counts.values()),
            "row_counts": stats.row_counts,
            "ledger": asdict(stats.ledger),
            "audit": asdict(stats.audit),
            "alembic_revision": stats.alembic_revision,
        }
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("dump", type=Path, help="the .dump.gpg file (manifest must sit next to it)")
    parser.add_argument("--passphrase-file", type=Path, help="for symmetric backups")
    parser.add_argument("--docker-container", help="run pg_restore inside this container")
    parser.add_argument("--keep", action="store_true", help="keep the restored database")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    try:
        if args.passphrase_file is not None:
            check_passphrase_file(args.passphrase_file)
        plan = RestorePlan(
            dump=args.dump,
            url=make_url(get_settings().database_url),
            # A public-key backup is decrypted with the secret key in gpg's keyring: no file.
            encryption=Encryption(passphrase_file=args.passphrase_file),
            tools=PgTools(args.docker_container),
            database=drill_database_name(datetime.now(UTC)),
            keep=args.keep,
        )
        sys.stdout.write("\n".join(plan.describe()) + "\n")
        if args.dry_run:
            sys.stdout.write("dry run: nothing was executed\n")
            return 0
        missing = plan.tools.missing_executables("pg_restore")
        if missing:
            raise BackupError(f"not on PATH: {', '.join(missing)}")
        for path in (plan.dump, plan.manifest_path):
            if not path.is_file():
                raise BackupError(f"not found: {path}")
        result = run_drill(plan)
    except BackupError as exc:
        sys.stderr.write(f"restore drill failed: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(report(result), indent=2) + "\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
