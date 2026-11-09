"""Database errors must not carry the values of a statement (a key, a password hash) into the
system log: every engine is created with `hide_parameters=True`."""

import pytest
from sqlalchemy import exc, text

import reprintbuddy.db as db
from reprintbuddy.db import make_engine, upgrade

SECRET = "geheimer-schluessel-4711"


def test_app_engine_error_does_not_show_values(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    with engine.begin() as conn:
        conn.execute(text("create table t (v text not null unique)"))
        conn.execute(text("insert into t (v) values (:v)"), {"v": SECRET})
    with pytest.raises(exc.IntegrityError) as caught:
        with engine.begin() as conn:
            conn.execute(text("insert into t (v) values (:v)"), {"v": SECRET})
    assert SECRET not in str(caught.value)
    assert "hidden due to hide_parameters" in str(caught.value)


def test_migration_engine_hides_values(tmp_path, monkeypatch):
    created = []
    original = db.create_engine

    def spy(*args, **kwargs):
        engine = original(*args, **kwargs)
        created.append(engine)
        return engine

    engine = make_engine(tmp_path / "t.db")
    monkeypatch.setattr(db, "create_engine", spy)
    upgrade(engine)
    assert created, "upgrade() creates its own migration engine"
    assert all(e.hide_parameters for e in created)
