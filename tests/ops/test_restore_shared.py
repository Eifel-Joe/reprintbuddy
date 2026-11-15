"""p531: the end of a restore, shared by the page and the command line (`ops/restore.py`).

What both ways do with it is pinned in tests/web/test_restore_both_ways.py; these are the pieces."""

from __future__ import annotations

from datetime import UTC, datetime

from reprintbuddy.ops import restore
from reprintbuddy.ops.status import UpdateStatus, write_status


def test_the_backup_a_failed_rollback_names_is_protected(tmp_path):
    named = tmp_path / "backups" / "reprintbuddy-v1.0.0-20260101T080000Z.db"
    write_status(tmp_path, UpdateStatus(state="rollback_failed", backup=str(named), started_at=datetime.now(UTC)))
    assert restore.protected_backups(tmp_path) == [named]


def test_nothing_is_protected_after_any_other_end_of_an_update(tmp_path):
    assert restore.protected_backups(tmp_path) == []  # no update has run
    for state in ("succeeded", "failed", "rolled_back"):
        write_status(tmp_path, UpdateStatus(state=state, backup=str(tmp_path / "b.db"), started_at=datetime.now(UTC)))
        assert restore.protected_backups(tmp_path) == [], state


def test_a_failed_rollback_without_a_backup_protects_nothing(tmp_path):
    write_status(tmp_path, UpdateStatus(state="rollback_failed", backup=None, started_at=datetime.now(UTC)))
    assert restore.protected_backups(tmp_path) == []
