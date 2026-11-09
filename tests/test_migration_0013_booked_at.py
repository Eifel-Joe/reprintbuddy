"""Revision 0013: every journal row has a booking date (Spec 6.2, p172); the saving time stays."""

import pytest
from sqlalchemy import inspect, text

from reprintbuddy.db import make_engine, upgrade


@pytest.fixture
def old_db(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision="0012")
    return engine


def test_existing_rows_are_booked_when_they_were_saved(old_db):
    with old_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO journal (id, at, actor, kind, qty) VALUES"
            " (1, '2026-11-01 08:30:00.000000', 'user', 'inventory', 4),"
            " (2, '2026-11-02 17:05:00.000000', 'auto', 'print_finished', 1)"
        ))
    upgrade(old_db, revision="0013")  # this file is about 0013, whatever comes after it
    with old_db.connect() as conn:
        rows = conn.execute(text("select id, at, booked_at, qty from journal order by id")).all()
    assert [(r.id, r.booked_at, r.qty) for r in rows] == [
        (1, "2026-11-01 08:30:00.000000", 4), (2, "2026-11-02 17:05:00.000000", 1),
    ]
    assert all(r.at == r.booked_at for r in rows)


def test_the_column_is_required_and_indexed(old_db):
    upgrade(old_db, revision="0013")  # this file is about 0013, whatever comes after it
    column = {c["name"]: c for c in inspect(old_db).get_columns("journal")}["booked_at"]
    assert column["nullable"] is False
    assert any(ix["column_names"] == ["booked_at"] for ix in inspect(old_db).get_indexes("journal"))


def test_a_second_run_changes_nothing(old_db):
    upgrade(old_db, revision="0013")  # this file is about 0013, whatever comes after it
    upgrade(old_db, revision="0013")
    with old_db.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == "0013"



def test_an_upgraded_database_has_the_structure_of_a_new_one(old_db, tmp_path):
    """The restore compares structures exactly (ops/fullbackup._structure): an installation that
    upgraded must look like a new one, foreign keys of the journal in the same order included."""
    from reprintbuddy.ops import fullbackup

    upgrade(old_db)
    old_db.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fullbackup._structure(tmp_path / "old.db") == fullbackup._structure(tmp_path / "new.db")
