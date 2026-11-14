"""The check of the rule "only add" for database steps of pre-releases (Spec 11.3a "Rückweg", 11.8).

A step after the head of the last stable version may add new tables (with everything that belongs to
them, references to existing rows included), add columns to existing tables by a plain ADD COLUMN that
are nullable or carry a default, and add indexes on new columns or new tables. Everything else is rework:
a rebuilt table, a changed or dropped column, rewritten rows, an index or unique index that covers an
existing column, a trigger on an existing table, a view. Its downgrade must bring back exactly what was
there: the same schema and the same rows.

`snapshot` takes everything that matters of a database apart from Alembic's own table; `adds_only_violations`
and `roundtrip_violations` compare two snapshots and say in words what differs. `fill_every_table` puts one
row into every table, read from the schema alone (foreign keys in order, a CHECK ... IN list gives the
value), so that a table added later is filled without anyone keeping a list.
Tests: tests/test_migration_rules.py.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

SKIPPED = ("alembic_version",)


def _norm(sql: str | None) -> str:
    return " ".join((sql or "").split())


@dataclass(frozen=True)
class Table:
    rootpage: int
    columns: tuple[tuple, ...]  # PRAGMA table_xinfo without the column id: name, type, notnull, default, pk, hidden
    foreign_keys: frozenset[tuple]
    sql: str


@dataclass
class Snapshot:
    tables: dict[str, Table] = field(default_factory=dict)
    indexes: dict[str, tuple] = field(default_factory=dict)  # name -> (table, columns, unique, sql)
    triggers: dict[str, tuple] = field(default_factory=dict)  # name -> (table, sql)
    views: dict[str, str] = field(default_factory=dict)
    rows: dict[str, list[tuple]] = field(default_factory=dict)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def snapshot(con: sqlite3.Connection) -> Snapshot:
    shot = Snapshot()
    objects = con.execute("SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master").fetchall()
    for kind, name, table, rootpage, sql in objects:
        if name.startswith("sqlite_") and kind == "table" or table in SKIPPED:
            continue
        if kind == "table":
            columns = tuple(tuple(row[1:]) for row in con.execute(f"PRAGMA table_xinfo({_quote(name)})"))
            keys = frozenset(tuple(row[2:5]) for row in con.execute(f"PRAGMA foreign_key_list({_quote(name)})"))
            shot.tables[name] = Table(rootpage, columns, keys, _norm(sql))
            rows = con.execute(f"SELECT * FROM {_quote(name)}").fetchall()
            shot.rows[name] = sorted(rows, key=repr)
        elif kind == "index":
            cols = tuple(row[2] for row in con.execute(f"PRAGMA index_info({_quote(name)})"))
            unique = next((row[2] for row in con.execute(f"PRAGMA index_list({_quote(table)})") if row[1] == name), 0)
            shot.indexes[name] = (table, cols, bool(unique), _norm(sql))
        elif kind == "trigger":
            shot.triggers[name] = (table, _norm(sql))
        elif kind == "view":
            shot.views[name] = _norm(sql)
    return shot


def snapshot_of(path: Path) -> Snapshot:
    con = sqlite3.connect(path)
    try:
        return snapshot(con)
    finally:
        con.close()


def adds_only_violations(before: Snapshot, after: Snapshot) -> list[str]:
    """What a step did beyond adding (empty: it only added)."""
    found: list[str] = []
    old_columns: dict[str, set[str]] = {}
    for name, old in before.tables.items():
        new = after.tables.get(name)
        if new is None:
            found.append(f"table {name}: dropped or renamed")
            continue
        old_columns[name] = {column[0] for column in old.columns}
        if new.rootpage != old.rootpage:
            found.append(f"table {name}: rebuilt (a new table copied over the old one)")
        if new.columns[: len(old.columns)] != old.columns:
            found.append(f"table {name}: existing columns changed, renamed or dropped")
            continue
        for column in new.columns[len(old.columns):]:
            column_name, _type, notnull, default = column[:4]
            if notnull and default is None:
                found.append(f"table {name}: new column {column_name} is NOT NULL without a default")
        if not old.foreign_keys <= new.foreign_keys:
            found.append(f"table {name}: existing references changed")
        width = len(old.columns)
        if sorted((row[:width] for row in after.rows[name]), key=repr) != before.rows[name]:
            found.append(f"table {name}: existing rows rewritten, deleted or added to")
    for name, old in before.indexes.items():
        if after.indexes.get(name) != old:
            found.append(f"index {name} on {old[0]}: changed or dropped")
    for name, new in after.indexes.items():
        if name in before.indexes:
            continue
        table, columns, unique = new[:3]
        touched = sorted(set(columns) & old_columns.get(table, set()))
        if touched:
            kind = "unique index" if unique else "index"
            found.append(f"{kind} {name} on existing column(s) {', '.join(touched)} of table {table}")
    for name, old in before.triggers.items():
        if after.triggers.get(name) != old:
            found.append(f"trigger {name} on {old[0]}: changed or dropped")
    for name, new in after.triggers.items():
        if name not in before.triggers and new[0] in before.tables:
            found.append(f"trigger {name} on existing table {new[0]}")
    if after.views != before.views:
        found.append("views added, changed or dropped")
    return found


def roundtrip_violations(before: Snapshot, back: Snapshot) -> list[str]:
    """What the downgrade did not bring back exactly (empty: schema and rows as before)."""
    found: list[str] = []
    for name in sorted(set(before.tables) | set(back.tables)):
        old, new = before.tables.get(name), back.tables.get(name)
        if old is None:
            found.append(f"table {name}: left behind")
        elif new is None:
            found.append(f"table {name}: missing")
        else:
            if (old.columns, old.foreign_keys, old.sql) != (new.columns, new.foreign_keys, new.sql):
                found.append(f"table {name}: schema differs")
            if before.rows[name] != back.rows[name]:
                found.append(f"table {name}: rows differ")
    for label, old_map, new_map in (
        ("index", before.indexes, back.indexes),
        ("trigger", before.triggers, back.triggers),
        ("view", before.views, back.views),
    ):
        for name in sorted(set(old_map) | set(new_map)):
            if old_map.get(name) != new_map.get(name):
                found.append(f"{label} {name}: differs")
    return found


# --- one row in every table, from the schema alone -----------------------------------------------

_IN_LIST = r"""\b"?{column}"?\s+IN\s*\(([^)]*)\)"""


def _value_for(table_sql: str, table: str, column: str, declared: str) -> object:
    allowed = re.search(_IN_LIST.format(column=re.escape(column)), table_sql, re.IGNORECASE)
    if allowed:
        first = allowed.group(1).split(",")[0].strip()
        return first.strip("'") if first.startswith("'") else float(first) if "." in first else int(first)
    kind = (declared or "").upper()
    if "BOOL" in kind:
        return 1
    if "INT" in kind:
        return 1
    if "DATETIME" in kind or "TIMESTAMP" in kind:
        return "2026-01-02 03:04:05.000000"
    if "DATE" in kind:
        return "2026-01-02"
    if any(word in kind for word in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return 1.5
    if "JSON" in kind:
        return "{}"
    return f"{table}.{column}"


def _order(con: sqlite3.Connection, tables: list[str]) -> list[str]:
    needs = {
        name: {row[2] for row in con.execute(f"PRAGMA foreign_key_list({_quote(name)})") if row[2] != name}
        for name in tables
    }
    ordered: list[str] = []
    while needs:
        ready = sorted(name for name, wanted in needs.items() if wanted <= set(ordered))
        if not ready:
            raise RuntimeError(f"foreign keys in a circle: {sorted(needs)}")
        ordered += ready
        for name in ready:
            del needs[name]
    return ordered


def fill_every_table(path: Path) -> None:
    """Insert one row into every table of the database (foreign keys point at the rows inserted before)."""
    con = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            if row[0] not in SKIPPED
        ]
        for table in _order(con, tables):
            table_sql = con.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
            references = {
                row[3]: (row[2], row[4]) for row in con.execute(f"PRAGMA foreign_key_list({_quote(table)})")
            }
            names, values, nullable = [], [], []
            info = con.execute(f"PRAGMA table_xinfo({_quote(table)})").fetchall()
            single_key = sum(1 for row in info if row[5]) == 1
            for _cid, column, declared, notnull, _default, pk, hidden in info:
                if hidden or (pk and single_key and "INT" in (declared or "").upper()):
                    continue  # generated columns; a single integer primary key numbers itself
                if column in references:
                    target, target_column = references[column]
                    if target == table:
                        if notnull:
                            raise RuntimeError(f"{table}.{column} refers to its own table and must be set")
                        continue
                    value = con.execute(
                        f"SELECT {_quote(target_column or 'id')} FROM {_quote(target)} LIMIT 1"
                    ).fetchone()[0]
                else:
                    value = _value_for(table_sql, table, column, declared)
                names.append(_quote(column))
                values.append(value)
                nullable.append(not notnull and not pk)
            insert = f"INSERT INTO {_quote(table)} ({', '.join(names)}) VALUES ({', '.join('?' for _ in values)})"
            try:
                con.execute(insert, values)
            except sqlite3.IntegrityError:
                # A CHECK across columns (e.g. "(field = 0) = (option_id IS NULL)"): the nullable ones empty.
                con.execute(insert, [None if empty else value for value, empty in zip(values, nullable)])
        con.commit()
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("the filled database breaks a reference")
    finally:
        con.close()
