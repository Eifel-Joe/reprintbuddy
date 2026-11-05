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
    assert [r[0] for r in rows] == ["0002"]


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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no umask to read back")
def test_umask_is_restored_after_creating_the_database(tmp_path):
    previous = os.umask(0o022)
    try:
        make_engine(tmp_path / "u.db")
    finally:
        current = os.umask(previous)
    assert current == 0o022


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_new_database_and_wal_files_are_private_from_the_start(tmp_path):
    previous = os.umask(0o022)  # a permissive umask, as many shells have
    try:
        engine = make_engine(tmp_path / "private" / "p.db")
        upgrade(engine)
    finally:
        os.umask(previous)
    names = [tmp_path / "private" / n for n in ("p.db", "p.db-wal", "p.db-shm")]
    for path in names:
        assert path.exists(), path
        assert os.stat(path).st_mode & 0o077 == 0, f"{path.name} is readable by others"
    assert os.stat(tmp_path / "private").st_mode & 0o077 == 0


def test_connections_can_be_used_from_worker_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    engine = make_engine(tmp_path / "t.db")
    with engine.connect() as conn:  # leaves one pooled connection behind
        conn.execute(text("select 1"))

    def other_thread():
        with engine.connect() as conn:
            return conn.execute(text("select 1")).scalar()

    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(other_thread).result() == 1


def test_a_read_does_not_pin_a_snapshot_that_breaks_a_later_write(tmp_path):
    """Read, wait, write: a commit by someone else in between must not make the write fail."""
    from reprintbuddy import settings as st

    engine = make_engine(tmp_path / "s.db")
    upgrade(engine)
    Session = make_sessionmaker(engine)
    waiting = Session()
    assert st.get_setting(waiting, "language") == "en"  # the read that starts the wait
    with Session() as other:
        st.set_setting(other, "language", "de")  # somebody else commits meanwhile
    st.set_setting(waiting, "queue_cap", 25)  # the real change comes afterwards
    waiting.close()
    with Session() as check:
        assert st.get_setting(check, "language") == "de"
        assert st.get_setting(check, "queue_cap") == 25
