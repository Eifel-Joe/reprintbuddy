"""Revision 0012: the activity log under Settings (Spec 10.7).

Two new tables: `activity` (one row per event) and `activity_marks` (the last recorded state of
what is only logged as a change, e.g. the connection to Bambuddy). Nothing older is touched.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from reprintbuddy import db as dbmod
from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.models import ActivityEntry, ActivityMark


def downgrade(engine, revision: str) -> None:
    """The mirror of `db.upgrade`, as in the test of revision 0011."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(dbmod.__file__).parent / "migrations"))
    migration_engine = create_engine(
        engine.url, future=True, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
    dbmod._configure(migration_engine, foreign_keys=False, transactional_ddl=True)
    try:
        with migration_engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, revision)
    finally:
        migration_engine.dispose()


@pytest.fixture
def old_db(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision="0011")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO settings (key, value) VALUES ('queue_cap', '7')"))
    return engine


def test_the_tables_are_new_and_empty(old_db):
    upgrade(old_db, revision="0012")
    names = inspect(old_db).get_table_names()
    assert "activity" in names and "activity_marks" in names
    with old_db.connect() as conn:
        assert conn.execute(text("select count(*) from activity")).scalar() == 0
        assert conn.execute(text("select count(*) from activity_marks")).scalar() == 0
        assert conn.execute(text("select value from settings where key = 'queue_cap'")).scalar() == "7"


def test_columns(old_db):
    upgrade(old_db, revision="0012")
    columns = {c["name"]: c for c in inspect(old_db).get_columns("activity")}
    assert set(columns) == {"id", "at", "area", "kind", "actor", "via", "address", "details"}
    for name in ("at", "area", "kind", "actor"):
        assert columns[name]["nullable"] is False, name
    for name in ("via", "address", "details"):
        assert columns[name]["nullable"] is True, name
    indexed = {tuple(index["column_names"]) for index in inspect(old_db).get_indexes("activity")}
    assert ("at",) in indexed and ("area",) in indexed
    marks = {c["name"] for c in inspect(old_db).get_columns("activity_marks")}
    assert marks == {"key", "state", "at"}


def _insert(conn, actor: str) -> None:
    conn.execute(
        text("insert into activity (at, area, kind, actor) values ('2026-11-09 12:00:00', 'login', 'login', :a)"),
        {"a": actor},
    )


@pytest.mark.parametrize("actor", ["user", "auto", "bambuddy", "system"])
def test_the_four_actors_are_allowed(old_db, actor):
    upgrade(old_db, revision="0012")
    with old_db.begin() as conn:
        _insert(conn, actor)


def test_no_other_actor(old_db):
    upgrade(old_db, revision="0012")
    with pytest.raises(IntegrityError):
        with old_db.begin() as conn:
            _insert(conn, "admin")


def test_one_mark_per_key(old_db):
    upgrade(old_db, revision="0012")
    insert = text("insert into activity_marks (key, state, at) values ('bambuddy.connection', :s, '2026-11-09 12:00:00')")
    with old_db.begin() as conn:
        conn.execute(insert, {"s": "ok"})
    with pytest.raises(IntegrityError):
        with old_db.begin() as conn:
            conn.execute(insert, {"s": "unreachable"})


def test_the_models_match_the_tables(tmp_path):
    engine = make_engine(tmp_path / "new.db")
    upgrade(engine)
    for model, table in ((ActivityEntry, "activity"), (ActivityMark, "activity_marks")):
        model_columns = {c.name for c in model.__table__.columns}
        table_columns = {c["name"] for c in inspect(engine).get_columns(table)}
        assert model_columns == table_columns, table


def test_downgrade_drops_the_tables_and_keeps_the_rest(old_db):
    upgrade(old_db, revision="0012")
    downgrade(old_db, "0011")
    names = inspect(old_db).get_table_names()
    assert "activity" not in names and "activity_marks" not in names
    with old_db.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == "0011"
        assert conn.execute(text("select value from settings where key = 'queue_cap'")).scalar() == "7"
