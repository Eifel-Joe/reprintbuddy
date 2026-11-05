"""SQLite engine, sessions and programmatic migrations.

The database path is the only setting that does not live in the database
itself; it comes from the command line (`--db`).
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

DEFAULT_DB_PATH = Path("/var/lib/reprintbuddy/reprintbuddy.db")

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _configure(engine: Engine, *, foreign_keys: bool, transactional_ddl: bool) -> None:
    """Connection setup shared by the application engine and the migration engine.

    pysqlite does not emit BEGIN before DDL and so runs it outside any
    transaction. For the migration engine, transaction control is taken away from
    the driver (isolation_level=None plus an explicit BEGIN), which makes DDL
    transactional: a failing migration then leaves nothing behind.

    The application engine must NOT do this. There every session would open a real
    transaction at its first read and keep that snapshot while it waits, for
    example for a slow answer from Bambuddy; a write after another request has
    committed then fails with "database is locked". With the driver's default
    behaviour a transaction only begins at the first write.
    """

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, _record):  # pragma: no cover - trivial
        if transactional_ddl:
            dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    if transactional_ddl:

        @event.listens_for(engine, "begin")
        def _begin(connection):  # pragma: no cover - trivial
            connection.exec_driver_sql("BEGIN")


def make_engine(db_path: Path) -> Engine:
    """Create the engine, the parent folder and (on POSIX) a 0o600 file."""
    db_path = Path(db_path)
    # A restrictive umask while the folder and the database (with its -wal and -shm
    # files, which copy the database file's mode) come into being, so they are never
    # readable by others, not even briefly. The umask is process-wide, hence only
    # held for this start-up step.
    previous_umask = os.umask(0o077)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{db_path}",
            future=True,
            # A pool only gets in the way with SQLite: opening a connection is cheap, and a
            # fixed-size pool can be exhausted (and freeze every request) by many waiting
            # requests. Each transaction opens its own connection and closes it afterwards.
            poolclass=NullPool,
            connect_args={"check_same_thread": False},  # sessions are used from worker threads
        )
        _configure(engine, foreign_keys=True, transactional_ddl=False)
        with engine.connect() as conn:  # connect once so the file exists
            conn.execute(text("select 1"))
    finally:
        os.umask(previous_umask)
    if os.name == "posix":
        os.chmod(db_path, 0o600)  # also for a database file that already existed
    return engine


def upgrade(engine: Engine, script_location: Path | None = None) -> None:
    """Run `alembic upgrade head` against this engine, without alembic.ini.

    The migration runs on its own connection with foreign keys switched off
    (SQLite cannot rebuild a referenced table otherwise; PRAGMA foreign_keys has
    no effect inside a transaction, so it is set before the first BEGIN), all in
    one transaction. Afterwards `PRAGMA foreign_key_check` must be clean, or
    everything is rolled back. `script_location` exists for tests.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(script_location or _MIGRATIONS_DIR))
    migration_engine = create_engine(
        engine.url, future=True, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
    _configure(migration_engine, foreign_keys=False, transactional_ddl=True)
    try:
        with migration_engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
            if connection.exec_driver_sql("PRAGMA foreign_key_check").all():
                raise RuntimeError("migration left foreign key violations; rolled back")
    finally:
        migration_engine.dispose()


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
