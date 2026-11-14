"""The test steps of tests/beta_steps.py run forward and, where they can, back (Spec 11.3a "Rückweg")."""

import sqlite3

import pytest
from beta_steps import STEPS, beta_scripts, revision_of

from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops.migrationsteps import current_revision, downgrade, head_of_dir


def tables(path):
    con = sqlite3.connect(path)
    try:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        con.close()


def columns(path, table):
    con = sqlite3.connect(path)
    try:
        return [row[1] for row in con.execute(f"PRAGMA table_xinfo({table})")]
    finally:
        con.close()


def migrated(tmp_path, scripts):
    path = tmp_path / "beta.db"
    engine = make_engine(path)
    upgrade(engine, scripts)
    engine.dispose()
    return path


def test_steps_are_appended_after_the_head_in_order(tmp_path):
    scripts = beta_scripts(tmp_path, "add_table", "add_column")
    assert head_of_dir(scripts / "versions") == revision_of(2)


def test_steps_that_only_add_go_forward_and_back(tmp_path):
    scripts = beta_scripts(tmp_path, "add_table", "add_column")
    path = migrated(tmp_path, scripts)
    assert "t_notes" in tables(path) and "t_flag" in columns(path, "orders")
    # op.drop_column without batch mode: SQLite's own DROP COLUMN, the table is not rebuilt
    downgrade(path, "0022", scripts)
    assert current_revision(path) == "0022"
    assert "t_notes" not in tables(path) and "t_flag" not in columns(path, "orders")


@pytest.mark.parametrize("name", sorted(STEPS))
def test_every_test_step_goes_forward(tmp_path, name):
    path = migrated(tmp_path, beta_scripts(tmp_path, name))
    assert current_revision(path) == revision_of(1)
