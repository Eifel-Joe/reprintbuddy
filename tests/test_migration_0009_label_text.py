"""Revision 0009: the article field "label text" (Spec 5.2, 8.5): free text for the label, optional.

NOTE for the merge of round v0.3.8: this revision was written while revision 0008 (part C) did not
exist yet, so it follows 0007. At the merge `down_revision` becomes "0008" (one line).
"""

import pytest
from sqlalchemy import inspect, text

from reprintbuddy.db import make_engine, upgrade


@pytest.fixture
def old_db(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision="0007")
    return engine


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def test_existing_articles_get_an_empty_label_text_and_keep_everything_else(old_db):
    with old_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO articles (id, name, library_file_id, plate_id, file_name, min_stock, stock)"
            " VALUES (1, 'Template A', 1, 1, 'a.gcode.3mf', 4, 7)"
        ))
    upgrade(old_db)
    assert scalar(old_db, "select version_num from alembic_version") == "0009"
    with old_db.connect() as conn:
        row = conn.execute(text("select name, min_stock, stock, label_text from articles where id = 1")).one()
    assert tuple(row) == ("Template A", 4, 7, None)


def test_the_column_is_text_and_optional(old_db):
    upgrade(old_db)
    column = {c["name"]: c for c in inspect(old_db).get_columns("articles")}["label_text"]
    assert column["nullable"] is True
    assert "CHAR" in str(column["type"]).upper() or "TEXT" in str(column["type"]).upper()


def test_a_second_run_changes_nothing(old_db):
    upgrade(old_db)
    upgrade(old_db)
    assert scalar(old_db, "select version_num from alembic_version") == "0009"
