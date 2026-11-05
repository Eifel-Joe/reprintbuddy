"""Migrations are transactional, run with foreign keys off, and match the models."""

import shutil
import textwrap
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from reprintbuddy import db as dbmod
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.models import Base, Setting


@pytest.fixture
def scripts(tmp_path) -> Path:
    """A private copy of the migration scripts that a test can extend."""
    target = tmp_path / "migrations"
    shutil.copytree(Path(dbmod.__file__).parent / "migrations", target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "m.db")
    upgrade(engine)  # the real revisions
    return engine


def add_revision(scripts: Path, body: str) -> None:
    (scripts / "versions" / "0900_test.py").write_text(
        textwrap.dedent(
            '''\
            from alembic import op
            import sqlalchemy as sa

            revision = "0900"
            down_revision = "0003"
            branch_labels = None
            depends_on = None


            def upgrade():
            '''
        )
        + textwrap.indent(textwrap.dedent(body), "    ")
        + "\n\n\ndef downgrade():\n    pass\n",
        encoding="utf-8",
    )


def version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("select version_num from alembic_version")).scalar()


def test_failing_revision_leaves_nothing_behind(engine, scripts):
    add_revision(
        scripts,
        """
        op.create_table("junk", sa.Column("id", sa.Integer(), primary_key=True))
        op.execute("INSERT INTO junk (id) VALUES (1)")
        raise RuntimeError("boom")
        """,
    )
    with pytest.raises(RuntimeError, match="boom"):
        upgrade(engine, script_location=scripts)
    assert "junk" not in inspect(engine).get_table_names()
    assert version(engine) == "0003"


def test_revision_that_breaks_foreign_keys_is_rolled_back(engine, scripts):
    add_revision(
        scripts,
        """
        op.create_table("parent", sa.Column("id", sa.Integer(), primary_key=True))
        op.create_table(
            "child",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("parent_id", sa.Integer(), sa.ForeignKey("parent.id")),
        )
        op.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")
        """,
    )
    with pytest.raises(RuntimeError, match="foreign key"):
        upgrade(engine, script_location=scripts)
    names = inspect(engine).get_table_names()
    assert "parent" not in names and "child" not in names
    assert version(engine) == "0003"


def test_batch_alter_keeps_rows_that_other_tables_reference(engine, scripts):
    """Rebuilding a referenced table needs foreign keys off while the migration runs."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, username, password_hash) VALUES (1, 'a', 'h')"))
    add_revision(
        scripts,
        """
        op.create_table(
            "child",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        )
        op.execute("INSERT INTO child (id, user_id) VALUES (1, 1)")
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("note", sa.String(), nullable=True))
        """,
    )
    upgrade(engine, script_location=scripts)
    assert version(engine) == "0900"
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from users")).scalar() == 1
        assert conn.execute(text("select user_id from child")).scalar() == 1
        assert conn.execute(text("pragma foreign_key_check")).all() == []
        assert conn.execute(text("pragma foreign_keys")).scalar() == 1  # normal connections enforce them


def test_application_connections_still_enforce_foreign_keys(engine):
    with engine.connect() as conn:
        assert conn.execute(text("pragma foreign_keys")).scalar() == 1


def test_models_and_migrations_do_not_drift(engine):
    with engine.connect() as conn:
        differences = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert differences == []


def test_session_rollback_discards_changes(engine):
    Session = make_sessionmaker(engine)
    with Session() as s:
        s.add(Setting(key="k", value='"v"'))
        s.flush()
        s.rollback()
    with Session() as s:
        assert s.get(Setting, "k") is None


def test_the_sessions_migration_adds_the_table_to_a_version_one_database(tmp_path, scripts):
    """An installation that ran revision 0001 gets the sessions table with 0002."""
    engine = make_engine(tmp_path / "old.db")
    only_first = tmp_path / "first_only"
    shutil.copytree(scripts, only_first)
    for path in [*(only_first / "versions").glob("0002*"), *(only_first / "versions").glob("0003*")]:
        path.unlink()
    upgrade(engine, script_location=only_first)
    assert version(engine) == "0001"
    assert "sessions" not in inspect(engine).get_table_names()
    upgrade(engine)  # the real scripts
    assert version(engine) == "0003"
    columns = {c["name"] for c in inspect(engine).get_columns("sessions")}
    assert columns == {"id", "created_at", "last_seen_at"}
