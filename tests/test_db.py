import os
import sys

import pytest
from sqlalchemy import inspect, text

from reprintbuddy.db import DEFAULT_DB_PATH, make_engine, make_sessionmaker, upgrade


def test_default_path_is_fixed():
    assert str(DEFAULT_DB_PATH).replace("\\", "/") == "/var/lib/reprintbuddy/reprintbuddy.db"


def test_upgrade_creates_tables(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    upgrade(engine)
    names = set(inspect(engine).get_table_names())
    assert {"settings", "users", "alembic_version"} <= names


def test_upgrade_twice_is_noop(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    upgrade(engine)
    upgrade(engine)
    with engine.connect() as conn:
        rows = conn.execute(text("select version_num from alembic_version")).all()
    assert [r[0] for r in rows] == ["0001"]


def test_foreign_keys_on(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    with engine.connect() as conn:
        assert conn.execute(text("pragma foreign_keys")).scalar() == 1


def test_wal_mode(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    with engine.connect() as conn:
        assert conn.execute(text("pragma journal_mode")).scalar() == "wal"


def test_creates_parent_dir(tmp_path):
    target = tmp_path / "a" / "b" / "t.db"
    engine = make_engine(target)
    upgrade(engine)
    assert target.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_file_mode_600(tmp_path):
    target = tmp_path / "t.db"
    make_engine(target)
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_sessionmaker_roundtrip(tmp_path):
    from reprintbuddy.models import Setting

    engine = make_engine(tmp_path / "t.db")
    upgrade(engine)
    Session = make_sessionmaker(engine)
    with Session() as s:
        s.add(Setting(key="k", value='"v"'))
        s.commit()
    with Session() as s:
        assert s.get(Setting, "k").value == '"v"'
