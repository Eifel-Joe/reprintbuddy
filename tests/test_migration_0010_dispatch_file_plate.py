"""Revision 0010: each print keeps the file and plate it was handed over with (Spec 7.6, v0.3.11 p53).

Rows from before stay without them: which file they were really sent with is unknown, and an empty
value means "search with the article's file", the behaviour of the versions before.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from reprintbuddy import db as dbmod
from reprintbuddy.db import make_engine, upgrade


def downgrade(engine, revision: str) -> None:
    """The mirror of `db.upgrade`: same connection set-up (foreign keys off, one transaction)."""
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
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        migration_engine.dispose()


@pytest.fixture
def old_db(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision="0009")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO articles (id, name, library_file_id, plate_id, file_name, min_stock, stock)"
            " VALUES (1, 'Template A', 1, 1, 'a.gcode.3mf', 4, 7)"
        ))
        conn.execute(text(
            "INSERT INTO dispatches (id, article_id, origin, status, units, queue_position, bambuddy_item_id)"
            " VALUES (1, 1, 'rule', 'queued', 2, 0, 42)"
        ))
    return engine


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def dispatch_columns(engine):
    return {c["name"]: c for c in inspect(engine).get_columns("dispatches")}


def test_existing_prints_get_no_file_and_keep_everything_else(old_db):
    upgrade(old_db, revision="0010")  # this file is about 0010, whatever comes after it
    assert scalar(old_db, "select version_num from alembic_version") == "0010"
    with old_db.connect() as conn:
        row = conn.execute(text(
            "select article_id, status, units, bambuddy_item_id, library_file_id, plate_id"
            " from dispatches where id = 1"
        )).one()
    assert tuple(row) == (1, "queued", 2, 42, None, None)


def test_the_columns_are_optional_numbers(old_db):
    upgrade(old_db, revision="0010")  # this file is about 0010, whatever comes after it
    columns = dispatch_columns(old_db)
    for name in ("library_file_id", "plate_id"):
        assert columns[name]["nullable"] is True
        assert "INT" in str(columns[name]["type"]).upper()


def test_a_second_run_changes_nothing(old_db):
    upgrade(old_db, revision="0010")  # this file is about 0010, whatever comes after it
    upgrade(old_db, revision="0010")
    assert scalar(old_db, "select version_num from alembic_version") == "0010"


def test_downgrade_drops_the_columns_and_keeps_the_print(old_db):
    upgrade(old_db, revision="0010")
    with old_db.begin() as conn:
        conn.execute(text("update dispatches set library_file_id = 3, plate_id = 2 where id = 1"))
    downgrade(old_db, "0009")
    assert scalar(old_db, "select version_num from alembic_version") == "0009"
    assert "library_file_id" not in dispatch_columns(old_db)
    assert scalar(old_db, "select bambuddy_item_id from dispatches where id = 1") == 42
