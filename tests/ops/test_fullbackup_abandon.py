"""A check that nobody waits for any more leaves no decrypted copy (v0.3.11 p140), and an error
of the machine is told from an error of the file (v0.3.11 p141).

Root of p140: a cancelled request does not stop its worker thread (`run_in_threadpool` raises
`CancelledError` at once, the thread goes on). The thread decrypts into `candidate-*.db` and then
rebuilds it; whatever the route removes meanwhile, the thread creates again. So the thread itself
watches a flag (`abandoned`) and removes what it made.
"""

from __future__ import annotations

import errno
import sqlite3
import threading
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops import backupcrypto as bc
from reprintbuddy.ops import fullbackup as fb

NOW = datetime(2026, 11, 8, 12, 0, 5, tzinfo=UTC)
FAST = bc.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)
PASSWORD = "abandon-password-1"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(fb, "KDF", FAST)


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "data" / "reprintbuddy.db"
    path.parent.mkdir()
    engine = make_engine(path)
    upgrade(engine)
    engine.dispose()
    maker = make_sessionmaker(make_engine(path))
    with maker() as session:
        st.set_setting(session, "queue_cap", 7)
        st.ensure_session_secret(session)
    return path


def made(live, work, password):
    created = fb.create_backup(live, work_dir=work, password=password, now=NOW)
    return created.path, fb.read_manifest(created.path)


def leftovers(work: Path, keep: Path | None = None) -> list[str]:
    return sorted(p.name for p in work.iterdir() if keep is None or p != keep)


@pytest.mark.parametrize("password", [None, PASSWORD])
def test_a_flag_set_before_the_start_stops_it_at_once(live, work, password):
    archive, manifest = made(live, work, password)
    target = work / "candidate-x.db"
    flag = threading.Event()
    flag.set()
    with pytest.raises(fb.Abandoned):
        fb.extract_database(archive, manifest, password, target, abandoned=flag)
    assert leftovers(work, archive) == []


@pytest.mark.parametrize("password", [None, PASSWORD])
def test_a_flag_set_while_writing_stops_it_and_removes_the_copy(live, work, password, monkeypatch):
    archive, manifest = made(live, work, password)
    target = work / "candidate-x.db"
    flag = threading.Event()
    copy = fb._copy_member

    def copy_then_abandon(archive_, name, sink, limit):
        def sinking(block):
            sink(block)
            flag.set()  # the request is cancelled after the first block reached the disk

        return copy(archive_, name, sinking, limit)

    monkeypatch.setattr(fb, "_copy_member", copy_then_abandon)
    with pytest.raises(fb.Abandoned):
        fb.extract_database(archive, manifest, password, target, abandoned=flag)
    assert leftovers(work, archive) == []


def test_without_a_flag_nothing_changes(live, work):
    archive, manifest = made(live, work, PASSWORD)
    target = work / "candidate-x.db"
    fb.extract_database(archive, manifest, PASSWORD, target)
    fb.verify_database(target, manifest, work_dir=work)
    assert target.is_file()


@pytest.mark.parametrize("stage", ["_copy_rows", "_check_settings"])
def test_a_flag_set_while_checking_leaves_no_candidate_rebuilt_or_trial(live, work, stage, monkeypatch):
    archive, manifest = made(live, work, None)
    target = work / "candidate-x.db"
    fb.extract_database(archive, manifest, None, target)
    flag = threading.Event()
    real = getattr(fb, stage)

    def then_abandon(*args, **kwargs):
        flag.set()  # cancelled while the worker is busy with the check
        return real(*args, **kwargs)

    monkeypatch.setattr(fb, stage, then_abandon)
    with pytest.raises(fb.Abandoned):
        fb.verify_database(target, manifest, work_dir=work, abandoned=flag)
    assert leftovers(work, archive) == []  # not even a candidate that the last step put in place


# --- machine or file (p141) --------------------------------------------------------------------


def _sqlite_error(code_name: str) -> sqlite3.Error:
    """A real error of SQLite with the real code: a full database for FULL, a missing file for CANTOPEN."""
    if code_name == "FULL":
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE t (x)")
        con.execute("PRAGMA max_page_count=2")
        try:
            for _ in range(10000):
                con.execute("INSERT INTO t VALUES (?)", ("x" * 1000,))
        except sqlite3.Error as error:
            return error
        raise AssertionError("no error")
    try:
        sqlite3.connect("/definitely/not/there/x.db")
    except sqlite3.Error as error:
        return error
    raise AssertionError("no error")


def test_the_probe_makes_real_codes():
    assert _sqlite_error("FULL").sqlite_errorname == "SQLITE_FULL"
    assert _sqlite_error("CANTOPEN").sqlite_errorname == "SQLITE_CANTOPEN"


def test_what_is_an_error_of_the_machine():
    from sqlalchemy.exc import OperationalError

    full = _sqlite_error("FULL")
    assert fb.is_environmental(OSError(errno.ENOSPC, "No space left on device"))
    assert fb.is_environmental(PermissionError())
    assert fb.is_environmental(full)
    assert fb.is_environmental(_sqlite_error("CANTOPEN"))
    assert fb.is_environmental(OperationalError("INSERT", {}, full))  # SQLAlchemy keeps it as `orig`
    assert not fb.is_environmental(sqlite3.DatabaseError("file is not a database"))
    assert not fb.is_environmental(sqlite3.OperationalError("disk I/O error"))  # no code: not told by words
    assert not fb.is_environmental(RuntimeError("x"))
    assert not fb.is_environmental(zlib.error("bad data"))
    assert not fb.is_environmental(fb.Damaged("x"))


@pytest.mark.parametrize("stage", ["_copy_rows", "_structure"])
def test_a_full_disk_while_checking_is_not_called_a_damaged_file(live, work, stage, monkeypatch):
    archive, manifest = made(live, work, None)
    target = work / "candidate-x.db"
    fb.extract_database(archive, manifest, None, target)
    full = _sqlite_error("FULL")

    def broken(*args, **kwargs):
        raise full

    monkeypatch.setattr(fb, stage, broken)
    with pytest.raises(sqlite3.OperationalError) as caught:
        fb.verify_database(target, manifest, work_dir=work)
    assert caught.value is full
    assert leftovers(work, archive) == [target.name]  # rebuilt and trial are gone; the candidate is the caller's


def test_a_full_disk_in_the_trial_migration_is_not_an_incompatible_database(live, work, monkeypatch):
    archive, manifest = made(live, work, None)
    target = work / "candidate-x.db"
    fb.extract_database(archive, manifest, None, target)

    def broken(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(fb, "upgrade", broken)
    with pytest.raises(OSError) as caught:
        fb.verify_database(target, manifest, work_dir=work)
    assert not isinstance(caught.value, fb.BackupError)


def test_a_damaged_file_is_still_a_damaged_file(live, work):
    archive, manifest = made(live, work, None)
    target = work / "candidate-x.db"
    fb.extract_database(archive, manifest, None, target)
    data = bytearray(target.read_bytes())
    data[4096 * 2 : 4096 * 2 + 200] = b"\xff" * 200  # a page in the middle
    target.write_bytes(bytes(data))
    with pytest.raises(fb.BackupError):
        fb.verify_database(target, manifest, work_dir=work)


# --- p149 (v0.3.12): making a backup for the download, and saving an upload, stop on the flag too ----------


@pytest.mark.parametrize("password", [None, PASSWORD])
def test_making_a_backup_with_the_flag_set_before_the_start_writes_nothing(live, work, password):
    flag = threading.Event()
    flag.set()
    target = work / "backup-x.zip"
    with pytest.raises(fb.Abandoned):
        fb.create_backup(live, work_dir=work, password=password, now=NOW, abandoned=flag, target=target)
    assert leftovers(work) == []


@pytest.mark.parametrize("password", [None, PASSWORD])
def test_making_a_backup_with_the_flag_set_while_writing_removes_the_file(live, work, password, monkeypatch):
    flag = threading.Event()
    write = fb._write_member
    seen: list[bool] = []

    def write_then_abandon(archive, info, blocks):
        write(archive, info, blocks)
        seen.append(True)
        flag.set()  # the request is cancelled once the manifest reached the disk

    monkeypatch.setattr(fb, "_write_member", write_then_abandon)
    target = work / "backup-x.zip"
    with pytest.raises(fb.Abandoned):
        fb.create_backup(live, work_dir=work, password=password, now=NOW, abandoned=flag, target=target)
    assert seen == [True]  # it stopped before the database member was written
    assert leftovers(work) == []


@pytest.mark.parametrize("password", [None, PASSWORD])
def test_making_a_backup_with_the_flag_set_after_the_last_block_removes_the_file(live, work, password, monkeypatch):
    flag = threading.Event()
    write = fb._write_member
    calls: list[str] = []

    def write_and_count(archive, info, blocks):
        write(archive, info, blocks)
        calls.append(info.filename)
        if len(calls) == 2:
            flag.set()  # everything is written; the flag comes right before the function returns

    monkeypatch.setattr(fb, "_write_member", write_and_count)
    target = work / "backup-x.zip"
    with pytest.raises(fb.Abandoned):
        fb.create_backup(live, work_dir=work, password=password, now=NOW, abandoned=flag, target=target)
    assert leftovers(work) == []


def test_making_a_backup_at_a_given_target_without_a_flag(live, work):
    target = work / "backup-given.zip"
    created = fb.create_backup(live, work_dir=work, password=None, now=NOW, target=target)
    assert created.path == target
    assert fb.read_manifest(target).encrypted is False
    assert leftovers(work) == ["backup-given.zip"]


def test_saving_an_upload_with_the_flag_set_writes_nothing(work):
    import io

    flag = threading.Event()
    flag.set()
    target = work / "upload-x.zip"
    with pytest.raises(fb.Abandoned):
        fb.save_upload(io.BytesIO(b"x" * 10), target, abandoned=flag)
    assert leftovers(work) == []


def test_saving_an_upload_with_the_flag_set_midway_removes_the_file(work):
    flag = threading.Event()

    class Source:
        def __init__(self):
            self.reads = 0

        def read(self, size):
            self.reads += 1
            if self.reads == 2:
                flag.set()  # the request is cancelled after the first block reached the disk
            return b"x" * 1024 if self.reads < 5 else b""

    target = work / "upload-x.zip"
    with pytest.raises(fb.Abandoned):
        fb.save_upload(Source(), target, abandoned=flag)
    assert leftovers(work) == []
