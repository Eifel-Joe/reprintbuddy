"""Revision 0011: what the push messages have reported (Spec 10.6 "Keine Flut", plan v0.3.12 B).

One row per occasion and thing: `kind` (the event) and `ref` (the print, article, item or warning).
Nothing older is touched, and a database without messages gets an empty table.
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
from reprintbuddy.models import NotifyMark


def downgrade(engine, revision: str) -> None:
    """The mirror of `db.upgrade`, as in the test of revision 0010."""
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
    upgrade(engine, revision="0010")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO settings (key, value) VALUES ('queue_cap', '7')"))
    return engine


def test_the_table_is_new_and_empty(old_db):
    upgrade(old_db, revision="0011")
    assert "notify_marks" in inspect(old_db).get_table_names()
    with old_db.connect() as conn:
        assert conn.execute(text("select count(*) from notify_marks")).scalar() == 0
        assert conn.execute(text("select value from settings where key = 'queue_cap'")).scalar() == "7"


def test_columns(old_db):
    upgrade(old_db, revision="0011")
    columns = {c["name"]: c for c in inspect(old_db).get_columns("notify_marks")}
    assert set(columns) == {
        "id", "kind", "ref", "state", "streak", "first_seen_at", "notified_at", "gone_since",
    }
    for name in ("kind", "ref", "state", "streak", "first_seen_at"):
        assert columns[name]["nullable"] is False, name
    for name in ("notified_at", "gone_since"):
        assert columns[name]["nullable"] is True, name


def test_one_row_per_kind_and_ref(old_db):
    upgrade(old_db, revision="0011")
    insert = text(
        "insert into notify_marks (kind, ref, state, streak, first_seen_at)"
        " values (:kind, :ref, 'due', 0, '2026-11-09 12:00:00')"
    )
    with old_db.begin() as conn:
        conn.execute(insert, {"kind": "paused", "ref": "1"})
        conn.execute(insert, {"kind": "below_min", "ref": "1"})  # same ref, other kind: fine
    with pytest.raises(IntegrityError):
        with old_db.begin() as conn:
            conn.execute(insert, {"kind": "paused", "ref": "1"})


def test_state_is_one_of_three(old_db):
    upgrade(old_db, revision="0011")
    with pytest.raises(IntegrityError):
        with old_db.begin() as conn:
            conn.execute(text(
                "insert into notify_marks (kind, ref, state, streak, first_seen_at)"
                " values ('paused', '1', 'lost', 0, '2026-11-09 12:00:00')"
            ))


def test_the_model_matches_the_table(tmp_path):
    engine = make_engine(tmp_path / "new.db")
    upgrade(engine)
    model_columns = {c.name for c in NotifyMark.__table__.columns}
    table_columns = {c["name"] for c in inspect(engine).get_columns("notify_marks")}
    assert model_columns == table_columns


def test_downgrade_drops_the_table_and_keeps_the_rest(old_db):
    upgrade(old_db, revision="0011")
    downgrade(old_db, "0010")
    assert "notify_marks" not in inspect(old_db).get_table_names()
    with old_db.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == "0010"
        assert conn.execute(text("select value from settings where key = 'queue_cap'")).scalar() == "7"
