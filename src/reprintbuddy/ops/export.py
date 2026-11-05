"""Export all data as JSON, without secrets (Spec 11.5a, 11.6).

Tables are read by reflection, so tables added by later building blocks are part
of the export without a change here. Left out: sensitive and internal settings,
the password hash and the table of signed-in browsers.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Connection, MetaData, Table, inspect, select, text

from reprintbuddy import __version__
from reprintbuddy.settings import NEVER_EXPORT

EXPORT_FORMAT = "reprintbuddy-export"
EXPORT_FORMAT_VERSION = 1

# API keys, the private HTTPS key and the cookie secret (see `settings.NEVER_EXPORT`).
SECRET_SETTING_KEYS: frozenset[str] = NEVER_EXPORT
EXCLUDED_COLUMNS: Mapping[str, frozenset[str]] = {"users": frozenset({"password_hash"})}
# Runtime state, not data: who is signed in right now.
EXCLUDED_TABLES: frozenset[str] = frozenset({"alembic_version", "sessions"})


def to_jsonable(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)  # the database stores UTC
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


def _rows(conn: Connection, name: str) -> list[dict[str, object]]:
    table = Table(name, MetaData(), autoload_with=conn)
    excluded = EXCLUDED_COLUMNS.get(name, frozenset())
    columns = [column for column in table.c if column.name not in excluded]
    order = [column for column in table.primary_key.columns if column.name not in excluded]
    result = conn.execute(select(*columns).order_by(*(order or columns)))
    rows: list[dict[str, object]] = []
    for row in result.mappings():
        record = {key: to_jsonable(value) for key, value in row.items()}
        if name == "settings":
            if record["key"] in SECRET_SETTING_KEYS:
                continue
            record["value"] = json.loads(str(record["value"]))
        rows.append(record)
    return rows


def export_data(conn: Connection, *, now: datetime) -> dict[str, object]:
    names = sorted(
        name for name in inspect(conn).get_table_names() if name not in EXCLUDED_TABLES
    )
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "app_version": __version__,
        "schema_revision": revision,
        "exported_at": now.isoformat(),
        "tables": {name: _rows(conn, name) for name in names},
    }


def export_bytes(conn: Connection, *, now: datetime) -> bytes:
    return json.dumps(export_data(conn, now=now), ensure_ascii=False, indent=2).encode("utf-8")
