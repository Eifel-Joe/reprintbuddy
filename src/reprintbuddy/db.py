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

DEFAULT_DB_PATH = Path("/var/lib/reprintbuddy/reprintbuddy.db")

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_engine(db_path: Path) -> Engine:
    """Create the engine, the parent folder and (on POSIX) a 0o600 file."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):  # pragma: no cover - trivial
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    # Connect once so the file exists, then restrict it to the service user.
    with engine.connect() as conn:
        conn.execute(text("select 1"))
    if os.name == "posix":
        os.chmod(db_path, 0o600)
    return engine


def upgrade(engine: Engine) -> None:
    """Run `alembic upgrade head` against this engine, without alembic.ini."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
