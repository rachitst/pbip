"""Generate (or verify) the ER diagram at docs/architecture/er-diagram.md from a migrated database.

The diagram is read from the live PostgreSQL catalogue, not from the SQLAlchemy models, so it shows
what the migrations actually built (TASK-106). Run it against a database at `alembic upgrade head`:

    uv run alembic -c apps/api/alembic.ini upgrade head
    uv run python scripts/er_diagram.py            # regenerate the committed diagram
    uv run python scripts/er_diagram.py --check    # exit 1 if the committed diagram is stale (CI)

The URL comes from --database-url, else DATABASE_URL via the validated settings. Output is
deterministic (everything sorted) so the committed file can be diff-checked.
"""

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Inspector

OUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "architecture" / "er-diagram.md"
SCHEMAS = ("public", "synthetic")
SCHEMA_TITLES = {
    "public": "Operational schema (`public`): the bank, its ledger, security and analytics",
    "synthetic": "ML dataset schema (`synthetic`): seeded synthetic customers and transactions",
}
SKIPPED_TABLES = {("public", "alembic_version")}


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    keys: list[str] = field(default_factory=list)


@dataclass
class ForeignKey:
    columns: list[str]
    target_schema: str
    target_table: str
    target_columns: list[str]
    on_delete: str
    nullable: bool
    unique: bool


@dataclass
class Table:
    schema: str
    name: str
    columns: list[Column]
    foreign_keys: list[ForeignKey]
    checks: list[str]
    partition_key: str
    triggers: list[str]

    @property
    def entity(self) -> str:
        return self.name if self.schema == "public" else f"{self.schema}_{self.name}"


def _mermaid_type(sql_type: str) -> str:
    """Mermaid attribute types must be one token: TIMESTAMP WITH TIME ZONE -> timestamptz."""
    t = sql_type.lower().replace("timestamp with time zone", "timestamptz")
    t = re.sub(r"\(.*?\)", "", t).replace("[]", "_array")
    return re.sub(r"\s+", "_", t.strip())


def _partition_children(conn: Connection, schema: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s AND c.relispartition"
        ),
        {"s": schema},
    )
    return {r[0] for r in rows}


def _partition_keys(conn: Connection, schema: str) -> dict[str, str]:
    """Partition key, not the partition list: the app adds monthly partitions at runtime."""
    rows = conn.execute(
        text(
            "SELECT c.relname, pg_get_partkeydef(c.oid) FROM pg_partitioned_table p "
            "JOIN pg_class c ON c.oid = p.partrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s"
        ),
        {"s": schema},
    )
    return {r[0]: str(r[1]) for r in rows}


def _triggers(conn: Connection, schema: str) -> dict[str, list[str]]:
    rows = conn.execute(
        text(
            "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s AND NOT t.tgisinternal AND NOT c.relispartition ORDER BY 1, 2"
        ),
        {"s": schema},
    )
    out: dict[str, list[str]] = {}
    for table, trigger in rows:
        out.setdefault(table, []).append(trigger)
    return out


def _unique_sets(insp: Inspector, schema: str, table: str, pk: list[str]) -> list[set[str]]:
    sets = [set(pk)] if pk else []
    sets += [set(u["column_names"]) for u in insp.get_unique_constraints(table, schema=schema)]
    sets += [
        {c for c in i["column_names"] if c is not None}
        for i in insp.get_indexes(table, schema=schema)
        if i["unique"] and not i.get("dialect_options", {}).get("postgresql_where")
    ]
    return sets


def _read_table(
    insp: Inspector, schema: str, name: str, partition_key: str, triggers: list[str]
) -> Table:
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]  # untyped factory in SQLAlchemy
    pk = list(insp.get_pk_constraint(name, schema=schema)["constrained_columns"] or [])
    uniques = _unique_sets(insp, schema, name, pk)
    raw_cols = insp.get_columns(name, schema=schema)
    nullable = {c["name"]: bool(c["nullable"]) for c in raw_cols}
    fks: list[ForeignKey] = []
    for fk in insp.get_foreign_keys(name, schema=schema):
        cols = list(fk["constrained_columns"])
        fks.append(
            ForeignKey(
                columns=cols,
                target_schema=fk.get("referred_schema") or schema,
                target_table=fk["referred_table"],
                target_columns=list(fk["referred_columns"]),
                on_delete=str((fk.get("options") or {}).get("ondelete") or "NO ACTION").upper(),
                nullable=any(nullable[c] for c in cols),
                unique=set(cols) in uniques,
            )
        )
    fk_cols = {c for fk in fks for c in fk.columns}
    single_uniques = {next(iter(u)) for u in uniques if len(u) == 1} - set(pk)
    columns = []
    for c in raw_cols:
        keys = [k for k, hit in (("PK", c["name"] in pk), ("FK", c["name"] in fk_cols)) if hit]
        if c["name"] in single_uniques:
            keys.append("UK")
        sql_type = c["type"].compile(dialect=dialect)
        columns.append(Column(c["name"], _mermaid_type(sql_type), bool(c["nullable"]), keys))
    checks = sorted(
        str(ck["name"]) for ck in insp.get_check_constraints(name, schema=schema) if ck["name"]
    )
    fks.sort(key=lambda f: (f.columns, f.target_table))
    return Table(schema, name, columns, fks, checks, partition_key, triggers)


def read_schema(database_url: str) -> list[Table]:
    engine = create_engine(database_url)
    tables: list[Table] = []
    with engine.connect() as conn:
        insp = inspect(conn)
        for schema in SCHEMAS:
            keys = _partition_keys(conn, schema)
            trigs = _triggers(conn, schema)
            children = _partition_children(conn, schema)
            for name in sorted(insp.get_table_names(schema=schema)):
                if (schema, name) in SKIPPED_TABLES or name in children:
                    continue
                tables.append(
                    _read_table(insp, schema, name, keys.get(name, "-"), trigs.get(name, []))
                )
    engine.dispose()
    return tables


def _cardinality(fk: ForeignKey) -> str:
    """Parent on the left. ||/|o: child must / may reference a parent; o{ many, o| at most one."""
    left = "|o" if fk.nullable else "||"
    right = "o|" if fk.unique else "o{"
    return f"{left}--{right}"


def _mermaid(tables: list[Table]) -> Iterable[str]:
    by_key = {(t.schema, t.name): t for t in tables}
    yield "```mermaid"
    yield "erDiagram"
    for t in tables:
        yield f"    {t.entity} {{"
        for c in t.columns:
            keys = f" {','.join(c.keys)}" if c.keys else ""
            note = "" if c.nullable or "PK" in c.keys else ' "not null"'
            yield f"        {c.type} {c.name}{keys}{note}"
        yield "    }"
    for t in tables:
        for fk in t.foreign_keys:
            parent = by_key.get((fk.target_schema, fk.target_table))
            target = parent.entity if parent else fk.target_table
            label = ", ".join(fk.columns)
            yield f'    {target} {_cardinality(fk)} {t.entity} : "{label}"'
    yield "```"


def _fk_text(fk: ForeignKey) -> str:
    target = f"{fk.target_table}({', '.join(fk.target_columns)})"
    return f"{', '.join(fk.columns)} -> {target}, {fk.on_delete}"


def _table_rows(tables: list[Table]) -> Iterable[str]:
    yield (
        "| Table | Primary key | Foreign keys (column -> target, ON DELETE) "
        "| CHECK constraints | Triggers | Partitioned by |"
    )
    yield "|---|---|---|---|---|---|"
    for t in tables:
        pk = ", ".join(c.name for c in t.columns if "PK" in c.keys) or "-"
        fks = "<br>".join(_fk_text(f) for f in t.foreign_keys) or "-"
        checks = "<br>".join(t.checks) or "-"
        trigs = "<br>".join(t.triggers) or "-"
        qualified = t.name if t.schema == "public" else f"{t.schema}.{t.name}"
        yield f"| `{qualified}` | {pk} | {fks} | {checks} | {trigs} | {t.partition_key} |"


def render(tables: list[Table], head: str) -> str:
    lines = [
        "# Entity-relationship diagram",
        "",
        "<!-- GENERATED FILE: do not edit by hand. Regenerate with scripts/er_diagram.py. -->",
        "",
        "Generated by `scripts/er_diagram.py` from the PostgreSQL catalogue of a database",
        f"migrated to Alembic revision `{head}` (TASK-106). CI regenerates it from the freshly",
        "migrated test database and fails when this file differs, so it always matches the",
        "migrations.",
        "",
        "Regenerate after any migration:",
        "",
        "```bash",
        "make infra-up",
        "uv run alembic -c apps/api/alembic.ini upgrade head",
        "uv run python scripts/er_diagram.py          # --check verifies without writing",
        "```",
        "",
        "Notation: `PK` primary key, `FK` foreign key, `UK` single-column unique.",
        "`||` the child row must reference a parent, `|o` the reference is optional (nullable FK),",
        "`o{` many, `o|` at most one. Relationship labels name the FK column(s). Types are",
        "simplified (lengths dropped). `alembic_version` and individual partitions are omitted;",
        "the partition key is listed per table below.",
        "",
    ]
    for schema in SCHEMAS:
        group = [t for t in tables if t.schema == schema]
        if not group:
            continue
        lines += [f"## {SCHEMA_TITLES[schema]}", "", f"{len(group)} tables.", ""]
        lines += list(_mermaid(group))
        lines += ["", "### Keys, constraints and triggers", ""]
        lines += list(_table_rows(group))
        lines.append("")
    return "\n".join(lines)


def _head(database_url: str) -> str:
    engine = create_engine(database_url)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    engine.dispose()
    return str(version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    parser.add_argument("--database-url", help="defaults to DATABASE_URL from the settings")
    args = parser.parse_args()
    url = args.database_url
    if not url:
        from pbip_api.core.config import get_settings

        url = get_settings().database_url
    fresh = render(read_schema(url), _head(url))
    if args.check:
        current = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if current != fresh:
            sys.stderr.write(
                "docs/architecture/er-diagram.md does not match the migrated schema. Run: "
                "uv run python scripts/er_diagram.py (against a database at head) and commit it\n"
            )
            return 1
        return 0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(fresh, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
