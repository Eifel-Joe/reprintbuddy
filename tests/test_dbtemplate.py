"""The migrated database the tests start from (refactoring before 1.0.0, R1).

Every test with an app or a `db` used to build its database from nothing and run every migration,
most of a second per test. The test run now migrates one database and copies it per test; these
tests make sure the copy is what a fresh migration gives, and that it never stands in for a
database a test set up itself or for a test about the app's own first start.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

import dbtemplate
from conftest import make_client
from reprintbuddy.db import make_engine, upgrade


def _contents(path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    try:
        schema = sorted(row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        rows = {name: sorted(connection.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr) for name in tables}
    finally:
        connection.close()
    return {"schema": schema, "rows": rows}


def test_template_matches_a_fresh_migration(tmp_path):
    fresh = tmp_path / "fresh.db"
    engine = make_engine(fresh)
    upgrade(engine)
    engine.dispose()
    copied = tmp_path / "copied.db"
    assert dbtemplate.copy_to(copied) is True
    assert _contents(copied) == _contents(fresh)
    assert _contents(copied)["rows"]["alembic_version"] == _contents(fresh)["rows"]["alembic_version"]


def test_existing_database_is_not_replaced(tmp_path):
    target = tmp_path / "old.db"
    target.write_bytes(b"old")
    assert dbtemplate.copy_to(target) is False
    assert target.read_bytes() == b"old"


def test_missing_folder_is_left_to_the_app(tmp_path):
    target = tmp_path / "x" / "t.db"
    assert dbtemplate.copy_to(target) is False
    assert not target.parent.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_copied_database_is_private(tmp_path):
    target = tmp_path / "t.db"
    previous = os.umask(0o022)  # a permissive umask, as many shells have
    try:
        assert dbtemplate.copy_to(target) is True
    finally:
        os.umask(previous)
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_make_client_starts_from_the_template(db_path):
    client = make_client(db_path)
    assert dbtemplate.last_copy == db_path
    assert client.get("/healthz").status_code == 200


def test_db_fixture_starts_from_the_template(db, tmp_path):
    assert dbtemplate.last_copy == tmp_path / "domain.db"


@pytest.mark.fresh_database
def test_a_fresh_database_test_lets_the_app_create_and_migrate_it(db_path):
    client = make_client(db_path)
    assert dbtemplate.last_copy != db_path
    assert client.get("/healthz").status_code == 200
    assert db_path.exists()
