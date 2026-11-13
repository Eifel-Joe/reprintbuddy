"""One migrated database per test run, copied for each test (refactoring before 1.0.0, R1).

Cause: every test with an app (`make_client`) or a `db` built its database from nothing and ran
every migration, about 0.7 s per test; with thousands of such tests that was the larger part of
the CI run. Fix: `tests/conftest.py` migrates `TEMPLATE` once per run; `copy_to` copies it to a
test's database path, and the app's own `upgrade` then finds nothing to do (about 0.05 s).
`copy_to` never touches a file a test made itself (an older database for a migration test) nor a
path whose folder does not exist yet (the app creates that folder privately), and a test marked
`fresh_database` gets no copy at all: there the app creates and migrates the file, as in production.
Rejected: one database per module with rollback per test, which would change what many tests see
(commits, the change feed, a second connection). Tests: tests/test_dbtemplate.py.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

TEMPLATE: Path | None = None  # set by the session fixture `_migrated_template` in conftest.py
last_copy: Path | None = None  # the last path `copy_to` filled, for tests/test_dbtemplate.py


def build(path: Path) -> Path:
    """Migrate a new database at `path` and leave it complete in one file (no -wal beside it)."""
    from reprintbuddy.db import make_engine, upgrade

    engine = make_engine(path)
    upgrade(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    leftover = path.with_name(path.name + "-wal")
    assert not leftover.exists() or leftover.stat().st_size == 0, "the template must be one complete file"
    return path


def copy_to(path: Path) -> bool:
    """Copy the template to `path` when there is none yet; True when it did."""
    global last_copy
    path = Path(path)
    if TEMPLATE is None or path.exists() or not path.parent.is_dir():
        return False
    shutil.copyfile(TEMPLATE, path)
    if sys.platform != "win32":
        os.chmod(path, 0o600)  # as `make_engine` creates it: readable by the owner only
    last_copy = path
    return True
