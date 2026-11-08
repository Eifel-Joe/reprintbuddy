"""Revision 0008: the two fields of the old batch hand-over are gone from `dispatches` (p56).

Since v0.3.5 every print goes to Bambuddy as a single queue entry. `external_ref` (NOT NULL UNIQUE,
only ever a random value) and `bambuddy_batch_id` were kept without a migration because nothing reads
them. Rows, their order, the inspections that point at them and the checks of the table stay.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from reprintbuddy import db as dbmod
from reprintbuddy.db import known_revisions, make_engine, upgrade

GONE = {"external_ref", "bambuddy_batch_id"}


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
    """A database at revision 0007 with prints of every kind."""
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision="0007")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO articles (id, name, library_file_id, plate_id, file_name)"
                " VALUES (1, 'Template A', 1, 1, 'a.gcode.3mf'), (2, 'Template B', 2, 1, 'b.gcode.3mf')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO dispatches (id, article_id, origin, external_ref, bambuddy_batch_id,"
                " bambuddy_item_id, status, units, queue_entry_id, queue_position, last_status, message)"
                " VALUES (1, 1, 'rule', 'ref-old-1', 77, 501, 'completed', 4, 9, 0, 'completed', NULL),"
                " (2, 1, 'manual', 'ref-old-2', 77, 502, 'failed', 4, NULL, 1, 'failed', 'put back'),"
                " (3, 2, 'rule', 'ref-new-3', NULL, 503, 'queued', 2, 11, 2, NULL, NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO inspections (id, dispatch_id, article_id, units, good_units)"
                " VALUES (1, 1, 1, 4, 3)"
            )
        )
    return engine


def columns(engine, table="dispatches"):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def rows(engine, sql):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql))]


def test_the_two_fields_are_gone_and_every_row_stays(old_db):
    assert GONE <= columns(old_db)
    upgrade(old_db, revision="0008")
    assert not GONE & columns(old_db)
    assert rows(
        old_db,
        "select id, article_id, origin, bambuddy_item_id, status, units, queue_entry_id,"
        " queue_position, last_status, message from dispatches order by id",
    ) == [
        (1, 1, "rule", 501, "completed", 4, 9, 0, "completed", None),
        (2, 1, "manual", 502, "failed", 4, None, 1, "failed", "put back"),
        (3, 2, "rule", 503, "queued", 2, 11, 2, None, None),
    ]


def test_what_points_at_a_print_still_does(old_db):
    upgrade(old_db, revision="0008")
    assert rows(old_db, "select dispatch_id, good_units from inspections") == [(1, 3)]
    assert rows(old_db, "pragma foreign_key_check") == []
    with pytest.raises(IntegrityError):  # one inspection per print
        with old_db.begin() as conn:
            conn.execute(text("INSERT INTO inspections (dispatch_id, article_id, units) VALUES (1, 1, 1)"))
    with pytest.raises(IntegrityError):  # the foreign key is enforced
        with old_db.begin() as conn:
            conn.execute(text("INSERT INTO inspections (dispatch_id, article_id, units) VALUES (99, 1, 1)"))


def test_the_checks_and_indexes_of_the_table_survive(old_db):
    upgrade(old_db, revision="0008")
    names = {i["name"] for i in inspect(old_db).get_indexes("dispatches")}
    assert {"ix_dispatches_status", "ix_dispatches_bambuddy_item_id"} <= names
    for bad in ("origin = 'nonsense'", "status = 'nonsense'", "units = 0"):
        with pytest.raises(IntegrityError):
            with old_db.begin() as conn:
                conn.execute(text(f"UPDATE dispatches SET {bad} WHERE id = 3"))
    assert not any(GONE & set(i["column_names"]) for i in inspect(old_db).get_indexes("dispatches"))
    assert not any(GONE & set(u["column_names"]) for u in inspect(old_db).get_unique_constraints("dispatches"))


def test_a_new_print_needs_neither_field(old_db):
    upgrade(old_db, revision="0008")
    with old_db.begin() as conn:
        for position in (3, 4):  # no unique value to collide on any more
            conn.execute(
                text(
                    "INSERT INTO dispatches (article_id, origin, status, units, queue_position)"
                    f" VALUES (1, 'rule', 'sending', 1, {position})"
                )
            )
    assert rows(old_db, "select count(*), max(id) from dispatches") == [(5, 5)]


def test_an_empty_database_migrates_too(tmp_path):
    engine = make_engine(tmp_path / "empty.db")
    upgrade(engine)
    assert not GONE & columns(engine)


def test_downgrade_puts_the_fields_back_with_a_distinct_value_per_row(old_db):
    upgrade(old_db, revision="0008")
    downgrade(old_db, "0007")
    assert GONE <= columns(old_db)
    refs = [r[0] for r in rows(old_db, "select external_ref from dispatches order by id")]
    assert len(refs) == 3 and len(set(refs)) == 3 and all(refs)
    assert rows(old_db, "select bambuddy_batch_id from dispatches order by id") == [(None,), (None,), (None,)]
    assert rows(old_db, "select id, status, units from dispatches order by id") == [
        (1, "completed", 4),
        (2, "failed", 4),
        (3, "queued", 2),
    ]
    assert rows(old_db, "select dispatch_id from inspections") == [(1,)]
    with pytest.raises(IntegrityError):  # NOT NULL UNIQUE as before
        with old_db.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO dispatches (article_id, origin, external_ref, status, units, queue_position)"
                    " VALUES (1, 'rule', :ref, 'sending', 1, 5)"
                ),
                {"ref": refs[0]},
            )


def test_up_down_up_gives_the_same_table(old_db):
    upgrade(old_db, revision="0008")
    first = inspect(old_db).get_columns("dispatches")
    downgrade(old_db, "0007")
    upgrade(old_db, revision="0008")
    again = inspect(old_db).get_columns("dispatches")
    assert [(c["name"], str(c["type"]), c["nullable"]) for c in first] == [
        (c["name"], str(c["type"]), c["nullable"]) for c in again
    ]
    assert rows(old_db, "select count(*) from dispatches") == [(3,)]
    assert rows(old_db, "pragma foreign_key_check") == []


def test_0008_sits_directly_on_0007():
    revisions = known_revisions()
    assert revisions[revisions.index("0007") + 1] == "0008"
    versions = Path(dbmod.__file__).parent / "migrations" / "versions"
    assert 'down_revision = "0007"' in next(versions.glob("0008*")).read_text(encoding="utf-8")
