"""Encrypted, verifiable logical backup of the PBIP database (TASK-083, runbook section 4.1).

    uv run python scripts/backup.py --out-dir DIR --passphrase-file FILE   # symmetric AES-256
    uv run python scripts/backup.py --out-dir DIR --recipient KEYID        # public-key encryption
    uv run python scripts/backup.py ... --docker-container NAME   # pg_dump inside the db container
    uv run python scripts/backup.py ... --dry-run                 # print the plan, touch nothing

``pg_dump -Fc`` is streamed straight into ``gpg`` so plaintext never reaches the disk. The dump
runs on a snapshot exported from a REPEATABLE READ transaction, and the same transaction counts
the rows, totals the ledger and reads the audit-chain head. The sidecar manifest therefore
describes exactly what is inside the dump, which is what lets ``scripts/restore.py`` prove that a
restore lost or altered nothing. The manifest holds counts, totals and hashes only: no customer
data, no credentials. The database URL comes from the validated settings (``DATABASE_URL``); the
password reaches libpq through ``PGPASSWORD`` in the child environment, never on a command line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import pbip_api.modules  # noqa: F401  (registers every model so cross-module FKs resolve)
from pbip_api.core.config import get_settings
from pbip_api.modules.accounts.models import Account, AccountType
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.ledger.models import EntryDirection, TransactionEntry
from sqlalchemy import Connection, case, create_engine, func, select, table, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

MANIFEST_VERSION = 1
DUMP_SUFFIX = ".dump.gpg"
MANIFEST_SUFFIX = ".manifest.json"
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HASH_BATCH = 1000


class BackupError(RuntimeError):
    """A backup or restore step failed; the message is safe to show (no secrets, no data)."""


# --------------------------------------------------------------------------- connection & tools


@dataclass(frozen=True)
class PgTarget:
    """Where libpq tools connect, from the SQLAlchemy URL. The password is kept out of repr."""

    host: str
    port: int
    user: str
    database: str
    password: str | None = field(default=None, repr=False)

    @classmethod
    def from_url(cls, url: URL) -> PgTarget:
        if not url.database or not url.username:
            raise BackupError("DATABASE_URL must name a user and a database")
        return cls(
            host=url.host or "127.0.0.1",
            port=url.port or 5432,
            user=url.username,
            database=url.database,
            password=url.password if isinstance(url.password, str) else None,
        )


@dataclass(frozen=True)
class PgTools:
    """Runs pg_dump/pg_restore on the host, or inside the database container with ``docker exec``.

    In container mode the tools connect over the container's local socket (the database is not
    published on a production host); the SQLAlchemy side still connects via ``DATABASE_URL`` and
    must reach the same server, otherwise the exported snapshot is unknown and pg_dump fails.
    """

    container: str | None = None

    def __post_init__(self) -> None:
        if self.container is not None and not _CONTAINER_NAME.match(self.container):
            raise BackupError("invalid --docker-container name")

    def command(self, tool: str, target: PgTarget, database: str, args: list[str]) -> list[str]:
        common = ["-U", target.user, "-d", database, "--no-password", *args]
        if self.container is None:
            return [tool, "-h", target.host, "-p", str(target.port), *common]
        return ["docker", "exec", "-i", "-e", "PGPASSWORD", self.container, tool, *common]

    def env(self, target: PgTarget) -> dict[str, str]:
        child = dict(os.environ)
        if target.password is not None:
            child["PGPASSWORD"] = target.password
        return child

    def missing_executables(self, tool: str) -> list[str]:
        needed = ["gpg", "docker" if self.container else tool]
        return [name for name in needed if shutil.which(name) is None]


@dataclass(frozen=True)
class Encryption:
    """Symmetric AES-256 with a passphrase file, or public-key encryption to a recipient.

    Decryption of a public-key backup needs neither: gpg finds the secret key in its keyring.

    gpg reads the passphrase file itself, so the passphrase never enters this process, an argv or
    a log line. ``--no-symkey-cache`` stops gpg-agent from caching it between runs.
    """

    passphrase_file: Path | None = None
    recipient: str | None = None

    def __post_init__(self) -> None:
        if self.passphrase_file is not None and self.recipient is not None:
            raise BackupError("use only one of --passphrase-file or --recipient")

    @property
    def mode(self) -> str:
        return "symmetric-aes256" if self.passphrase_file else "public-key"

    def _base(self) -> list[str]:
        args = ["gpg", "--batch", "--yes", "--quiet", "--no-symkey-cache"]
        if self.passphrase_file is not None:
            args += ["--pinentry-mode", "loopback", "--passphrase-file", str(self.passphrase_file)]
        return args

    def encrypt_command(self) -> list[str]:
        if self.recipient is not None:
            return [*self._base(), "--encrypt", "--recipient", self.recipient]
        if self.passphrase_file is None:
            raise BackupError("encryption needs --passphrase-file or --recipient")
        return [*self._base(), "--symmetric", "--cipher-algo", "AES256"]

    def decrypt_command(self, source: Path) -> list[str]:
        return [*self._base(), "--decrypt", str(source)]


def check_passphrase_file(path: Path) -> None:
    """Refuse a missing or group/world-readable passphrase file (it unlocks every backup)."""
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise BackupError(f"passphrase file not readable: {path}") from exc
    if not stat.S_ISREG(mode):
        raise BackupError(f"passphrase file is not a regular file: {path}")
    if mode & 0o077:
        raise BackupError(f"passphrase file must be chmod 600 (is {stat.S_IMODE(mode):o}): {path}")


def run_pipeline(
    producer: list[str],
    consumer: list[str],
    env: dict[str, str],
    stdout: IO[bytes] | int = subprocess.DEVNULL,
) -> tuple[int, int]:
    """``producer | consumer`` without a shell. Returns both exit codes; the caller decides."""
    with subprocess.Popen(producer, stdout=subprocess.PIPE, env=env) as first:
        with subprocess.Popen(consumer, stdin=first.stdout, stdout=stdout, env=env) as second:
            if first.stdout is not None:
                first.stdout.close()  # so the producer sees SIGPIPE if the consumer dies
            second_code = second.wait()
        first_code = first.wait()
    return first_code, second_code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def shell_line(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


# --------------------------------------------------------------------------- statistics


@dataclass(frozen=True)
class LedgerStats:
    accounts: int
    customer_balance_total: int  # minor units, non-SYSTEM accounts
    held_total: int
    net_balance: int  # every account incl. SYSTEM; zero when every posted transaction balances
    balances_sha256: str  # over (id, ledger_balance, held_amount) of every account, by id
    unreconciled_accounts: int  # ledger_balance != CREDIT - DEBIT of its entries
    unbalanced_transactions: int  # sum(DEBIT) != sum(CREDIT) within one transaction


@dataclass(frozen=True)
class AuditStats:
    rows: int
    head_seq: int | None
    head_hash: str | None
    chain_valid: bool
    chain_checked: int
    first_invalid_seq: int | None


@dataclass(frozen=True)
class DatabaseStats:
    alembic_revision: str | None
    row_counts: dict[str, int]
    ledger: LedgerStats
    audit: AuditStats

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatabaseStats:
        return cls(
            alembic_revision=data["alembic_revision"],
            row_counts={str(k): int(v) for k, v in data["row_counts"].items()},
            ledger=LedgerStats(**data["ledger"]),
            audit=AuditStats(**data["audit"]),
        )


def _row_counts(connection: Connection) -> dict[str, int]:
    tables = connection.execute(
        text(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' "
            "AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2"
        )
    ).all()
    counts: dict[str, int] = {}
    for schema, name in tables:
        counted = connection.execute(
            select(func.count()).select_from(table(name, schema=schema))
        ).scalar_one()
        counts[f"{schema}.{name}"] = int(counted)
    return counts


def _balances_digest(connection: Connection) -> str:
    digest = hashlib.sha256()
    rows = connection.execution_options(yield_per=_HASH_BATCH).execute(
        select(Account.id, Account.ledger_balance, Account.held_amount).order_by(Account.id)
    )
    for account_id, balance, held in rows:
        digest.update(f"{account_id}:{balance}:{held}\n".encode())
    return digest.hexdigest()


def _ledger_stats(connection: Connection) -> LedgerStats:
    customer = Account.account_type != AccountType.SYSTEM
    totals = connection.execute(
        select(
            func.count(),
            func.coalesce(func.sum(case((customer, Account.ledger_balance), else_=0)), 0),
            func.coalesce(func.sum(Account.held_amount), 0),
            func.coalesce(func.sum(Account.ledger_balance), 0),
        )
    ).one()
    is_credit = TransactionEntry.direction == EntryDirection.CREDIT
    signed = case((is_credit, TransactionEntry.amount), else_=-TransactionEntry.amount)
    per_account = (
        select(TransactionEntry.account_id, func.sum(signed).label("total"))
        .group_by(TransactionEntry.account_id)
        .subquery()
    )
    unreconciled = connection.execute(
        select(func.count())
        .select_from(Account)
        .outerjoin(per_account, per_account.c.account_id == Account.id)
        .where(Account.ledger_balance != func.coalesce(per_account.c.total, 0))
    ).scalar_one()
    unbalanced_ids = (
        select(TransactionEntry.transaction_id)
        .group_by(TransactionEntry.transaction_id)
        .having(func.sum(signed) != 0)
        .subquery()
    )
    unbalanced = connection.execute(select(func.count()).select_from(unbalanced_ids)).scalar_one()
    return LedgerStats(
        accounts=int(totals[0]),
        customer_balance_total=int(totals[1]),
        held_total=int(totals[2]),
        net_balance=int(totals[3]),
        balances_sha256=_balances_digest(connection),
        unreconciled_accounts=int(unreconciled),
        unbalanced_transactions=int(unbalanced),
    )


def _audit_stats(connection: Connection) -> AuditStats:
    rows = connection.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    head = connection.execute(
        select(AuditLog.seq, AuditLog.hash).order_by(AuditLog.seq.desc()).limit(1)
    ).one_or_none()
    # The project's own verifier (the code behind GET /api/v1/compliance/audit-logs/verify),
    # bound to this connection so it reads the same snapshot. Read-only; never committed.
    with Session(bind=connection) as session:
        chain = AuditService(session).verify_chain()
    return AuditStats(
        rows=int(rows),
        head_seq=int(head[0]) if head else None,
        head_hash=str(head[1]) if head else None,
        chain_valid=chain.valid,
        chain_checked=chain.checked,
        first_invalid_seq=chain.first_invalid_seq,
    )


def collect_stats(connection: Connection) -> DatabaseStats:
    """Everything the restore compares, read in the caller's (snapshot) transaction."""
    revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return DatabaseStats(
        alembic_revision=str(revision) if revision is not None else None,
        row_counts=_row_counts(connection),
        ledger=_ledger_stats(connection),
        audit=_audit_stats(connection),
    )


def integrity_problems(stats: DatabaseStats) -> list[str]:
    """Invariants that must hold in any healthy copy, independent of the manifest."""
    problems: list[str] = []
    if not stats.audit.chain_valid:
        problems.append(f"audit hash chain broken at seq {stats.audit.first_invalid_seq}")
    elif stats.audit.chain_checked != stats.audit.rows:
        problems.append(
            f"audit chain verified {stats.audit.chain_checked} rows of {stats.audit.rows}"
        )
    if stats.ledger.unreconciled_accounts:
        problems.append(f"{stats.ledger.unreconciled_accounts} accounts do not match their entries")
    if stats.ledger.unbalanced_transactions:
        problems.append(f"{stats.ledger.unbalanced_transactions} transactions do not balance")
    if stats.ledger.net_balance != 0:
        problems.append(f"ledger net balance is {stats.ledger.net_balance}, expected 0")
    return problems


# --------------------------------------------------------------------------- manifest


@dataclass(frozen=True)
class Manifest:
    created_at: str
    database: str
    dump_file: str
    dump_sha256: str
    dump_bytes: int
    encryption: str
    server_version: str
    stats: DatabaseStats
    version: int = MANIFEST_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        try:
            data = json.loads(raw)
            if data.get("version") != MANIFEST_VERSION:
                raise BackupError(f"unsupported manifest version {data.get('version')!r}")
            return cls(
                created_at=str(data["created_at"]),
                database=str(data["database"]),
                dump_file=str(data["dump_file"]),
                dump_sha256=str(data["dump_sha256"]),
                dump_bytes=int(data["dump_bytes"]),
                encryption=str(data["encryption"]),
                server_version=str(data["server_version"]),
                stats=DatabaseStats.from_dict(data["stats"]),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise BackupError("manifest is malformed") from exc


def backup_stem(database: str, now: datetime) -> str:
    """``pbip-<db>-<UTC timestamp>``; sorts chronologically and never collides within a second."""
    if now.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return f"pbip-{database}-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}"


def manifest_path_for(dump: Path) -> Path:
    name = dump.name
    if not name.endswith(DUMP_SUFFIX):
        raise BackupError(f"backup file name must end in {DUMP_SUFFIX}")
    return dump.with_name(name[: -len(DUMP_SUFFIX)] + MANIFEST_SUFFIX)


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


# --------------------------------------------------------------------------- backup


@dataclass(frozen=True)
class BackupPlan:
    url: URL
    out_dir: Path
    encryption: Encryption
    tools: PgTools
    now: datetime

    @property
    def target(self) -> PgTarget:
        return PgTarget.from_url(self.url)

    @property
    def dump_path(self) -> Path:
        return self.out_dir / (backup_stem(self.target.database, self.now) + DUMP_SUFFIX)

    def dump_command(self, snapshot: str) -> list[str]:
        args = ["--format=custom", f"--snapshot={snapshot}"]
        return self.tools.command("pg_dump", self.target, self.target.database, args)

    def describe(self) -> list[str]:
        return [
            f"source:   {self.url.render_as_string(hide_password=True)}",
            "step 1:   open a REPEATABLE READ transaction, SELECT pg_export_snapshot()",
            "step 2:   in that snapshot: row counts of every table, ledger totals and "
            "reconciliation, audit-chain head and AuditService.verify_chain()",
            f"step 3:   {shell_line(self.dump_command('<snapshot>'))}",
            f"          | {shell_line(self.encryption.encrypt_command())} > {self.dump_path}",
            f"step 4:   sha256 of the encrypted file -> {manifest_path_for(self.dump_path)}",
        ]


def _dump_into(plan: BackupPlan, snapshot: str, partial: Path) -> None:
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        dump_code, gpg_code = run_pipeline(
            plan.dump_command(snapshot),
            plan.encryption.encrypt_command(),
            plan.tools.env(plan.target),
            stdout=out,
        )
    if dump_code != 0 or gpg_code != 0:
        raise BackupError(f"pg_dump exited {dump_code}, gpg exited {gpg_code}")


def run_backup(plan: BackupPlan) -> Manifest:
    """Dump, encrypt and describe one database. Leaves no partial file behind on failure."""
    dump_path = plan.dump_path
    partial = dump_path.with_name(dump_path.name + ".partial")
    if dump_path.exists():
        raise BackupError(f"refusing to overwrite {dump_path}")
    engine = create_engine(plan.url, isolation_level="REPEATABLE READ")
    try:
        with engine.connect() as connection, connection.begin():
            snapshot = str(connection.execute(text("SELECT pg_export_snapshot()")).scalar_one())
            server_version = str(connection.execute(text("SHOW server_version")).scalar_one())
            stats = collect_stats(connection)
            _dump_into(plan, snapshot, partial)  # the snapshot lives until this commits
        partial.rename(dump_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        engine.dispose()
    manifest = Manifest(
        created_at=plan.now.astimezone(UTC).isoformat(),
        database=plan.target.database,
        dump_file=dump_path.name,
        dump_sha256=sha256_file(dump_path),
        dump_bytes=dump_path.stat().st_size,
        encryption=plan.encryption.mode,
        server_version=server_version,
        stats=stats,
    )
    _write_private(manifest_path_for(dump_path), manifest.to_json())
    return manifest


def _summary(manifest: Manifest, seconds: float) -> dict[str, Any]:
    return {
        "dump_file": manifest.dump_file,
        "dump_bytes": manifest.dump_bytes,
        "dump_sha256": manifest.dump_sha256,
        "seconds": round(seconds, 1),
        "tables": len(manifest.stats.row_counts),
        "rows": sum(manifest.stats.row_counts.values()),
        "customer_balance_total_minor": manifest.stats.ledger.customer_balance_total,
        "audit_rows": manifest.stats.audit.rows,
        "audit_chain_valid": manifest.stats.audit.chain_valid,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out-dir", type=Path, required=True, help="directory for the backup")
    key = parser.add_mutually_exclusive_group(required=True)
    key.add_argument("--passphrase-file", type=Path, help="chmod 600 file holding the passphrase")
    key.add_argument("--recipient", help="gpg key id/fingerprint to encrypt to")
    parser.add_argument("--docker-container", help="run pg_dump inside this container")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    try:
        if args.passphrase_file is not None:
            check_passphrase_file(args.passphrase_file)
        plan = BackupPlan(
            url=make_url(get_settings().database_url),
            out_dir=args.out_dir,
            encryption=Encryption(args.passphrase_file, args.recipient),
            tools=PgTools(args.docker_container),
            now=datetime.now(UTC),
        )
        sys.stdout.write("\n".join(plan.describe()) + "\n")
        if args.dry_run:
            sys.stdout.write("dry run: nothing was executed\n")
            return 0
        missing = plan.tools.missing_executables("pg_dump")
        if missing:
            raise BackupError(f"not on PATH: {', '.join(missing)}")
        if not plan.out_dir.is_dir():
            raise BackupError(f"output directory does not exist: {plan.out_dir}")
        started = time.monotonic()
        manifest = run_backup(plan)
    except BackupError as exc:
        sys.stderr.write(f"backup failed: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(_summary(manifest, time.monotonic() - started), indent=2) + "\n")
    problems = integrity_problems(manifest.stats)
    if problems:
        sys.stderr.write("backup written, but the SOURCE failed integrity checks:\n")
        sys.stderr.write("".join(f"  - {problem}\n" for problem in problems))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
