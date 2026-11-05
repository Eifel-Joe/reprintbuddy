import warnings

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import QueuePool

from reprintbuddy import db as dbmod
from reprintbuddy.db import WriteSessionMaker, begin_immediate, make_engine, make_sessionmaker, upgrade
from reprintbuddy.models import Setting


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "i.db")
    upgrade(engine)
    return engine


def test_begin_immediate_after_a_read_is_refused_not_ignored(engine):
    """SQLAlchemy only warns when the session already holds a connection; a write
    transaction that silently is not immediate would lose updates, so it must raise."""
    session = make_sessionmaker(engine)()
    try:
        session.execute(select(Setting)).all()  # the session now holds a connection
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with pytest.raises(RuntimeError):
                begin_immediate(session)
    finally:
        session.close()


def test_begin_immediate_as_first_statement_works(engine):
    session = make_sessionmaker(engine)()
    try:
        with session.begin():
            begin_immediate(session)
            assert session.connection().get_execution_options().get("immediate") is True
    finally:
        session.close()


def test_driver_isolation_level_is_restored_when_the_connection_goes_back(tmp_path):
    """Taking transaction control from the driver for one write transaction must not outlive it,
    also with a pool that reuses connections (no coupling to NullPool)."""
    base = make_engine(tmp_path / "p.db")
    upgrade(base)
    pooled = create_engine(base.url, poolclass=QueuePool, pool_size=1, connect_args={"check_same_thread": False})
    dbmod._configure(pooled, foreign_keys=True, transactional_ddl=False)
    try:
        raw = pooled.raw_connection()
        default = raw.driver_connection.isolation_level
        raw.close()
        assert default is not None
        maker = WriteSessionMaker(pooled, expire_on_commit=False)
        with maker.begin() as session:
            session.add(Setting(key="k", value='"v"'))
        assert pooled.pool.checkedout() == 0
        raw = pooled.raw_connection()  # the same connection, reused from the pool
        assert raw.driver_connection.isolation_level == default
        raw.close()
        # a normal (reading or lazily writing) session on the reused connection still commits
        with make_sessionmaker(pooled)() as plain:
            plain.add(Setting(key="k2", value='"w"'))
            plain.commit()
        with make_sessionmaker(pooled)() as check:
            assert {s.key for s in check.scalars(select(Setting))} >= {"k", "k2"}
    finally:
        pooled.dispose()
