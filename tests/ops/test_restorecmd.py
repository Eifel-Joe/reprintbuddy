"""`reprintbuddy restore-backup <file>`: the emergency way to restore a full backup (p100)."""

import json
import socket
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FakeRunner

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.cli import main
from reprintbuddy.db import known_revisions, make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops import backupcrypto as bc
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.ops import restorecmd
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.service import SERVICE, SYSTEMCTL, UPDATE_SERVICE

NOW = datetime(2026, 11, 8, 9, 30, 0, tzinfo=UTC)
FAST = bc.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)
SECRET_BACKUP_PASSWORD = "backup-password-ZZZ-1"
API_KEY = "bb_RESTORECMD_key"
IS_ACTIVE = (SYSTEMCTL, "is-active", SERVICE)


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(fb, "KDF", FAST)


def make_db(path: Path, revision: str = "head") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(path)
    upgrade(engine, revision=revision)
    engine.dispose()
    return path


def fill(path: Path, *, cap: int, key: str = API_KEY) -> Path:
    maker = make_sessionmaker(make_engine(path))
    with maker() as session:
        st.set_setting(session, "queue_cap", cap)
        st.set_setting(session, "bambuddy_api_key", key)
        st.ensure_session_secret(session)
        auth.create_admin(session, "admin", "admin-password-1")
        auth.create_session(session)
    return path


@pytest.fixture
def source(tmp_path):
    """The database a backup is made of (queue cap 7)."""
    return fill(make_db(tmp_path / "source" / "reprintbuddy.db"), cap=7)


@pytest.fixture
def target(tmp_path):
    """The database that is in place and gets replaced (queue cap 3)."""
    return fill(make_db(tmp_path / "data" / "reprintbuddy.db"), cap=3, key="bb_OLD_key")


def backup_of(source: Path, tmp_path: Path, password: str | None = None) -> Path:
    work = tmp_path / "making"
    work.mkdir(exist_ok=True)
    created = fb.create_backup(source, work_dir=work, password=password, now=NOW)
    final = tmp_path / f"given-{created.filename}"
    created.path.replace(final)
    return final


def rewritten(good: Path, target: Path, **changes) -> Path:
    """A copy of a backup with members replaced; `manifest` is a function of the parsed manifest."""
    with zipfile.ZipFile(good) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    for name, data in changes.items():
        if name == "manifest":
            parsed = json.loads(entries["manifest.json"])
            data(parsed)
            entries["manifest.json"] = json.dumps(parsed).encode()
        else:
            entries[name] = data
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return target


def cap_of(path: Path):
    con = sqlite3.connect(path)
    try:
        return json.loads(con.execute("SELECT value FROM settings WHERE key = 'queue_cap'").fetchone()[0])
    finally:
        con.close()


def count(path: Path, table: str) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


class Said(list):
    def text(self) -> str:
        return "\n".join(self)


def run(archive, db_path, *, runner=None, passwords=(), probe=lambda: None):
    """Call the restore with nothing running (`probe` says no) unless the test says otherwise."""
    said = Said()
    asked = []
    queue = list(passwords)

    def ask():
        asked.append(1)
        return queue.pop(0) if queue else ""

    code = restorecmd.restore_backup(
        archive,
        db_path,
        runner=runner or FakeRunner(),
        ask_password=ask,
        say=said.append,
        now=NOW,
        probe=probe,
    )
    return code, said, asked


def nothing_left_behind(db_path: Path):
    work = fb.work_dir_for(db_path)
    assert not work.exists() or not list(work.glob("*")), list(work.glob("*"))


# --- the restore --------------------------------------------------------------------------------


def test_a_plain_backup_replaces_the_database_and_says_what_happened(tmp_path, source, target):
    archive = backup_of(source, tmp_path)
    code, said, asked = run(archive, target)
    assert code == 0, said.text()
    assert cap_of(target) == 7
    assert asked == []  # a backup without a password asks for none
    assert count(target, "sessions") == 0  # everybody signs in again
    saved = list((target.parent / "backups").glob("reprintbuddy-before-restore-*.db"))
    assert len(saved) == 1 and cap_of(saved[0]) == 3  # the database that was there
    text = said.text()
    assert "Restored the backup" in text and str(saved[0]) in text
    assert "systemctl start reprintbuddy" in text
    assert "sign in again" in text
    nothing_left_behind(target)


def test_the_restored_database_is_the_one_of_the_backup_and_opens(tmp_path, source, target):
    code, _, _ = run(backup_of(source, tmp_path), target)
    assert code == 0
    maker = make_sessionmaker(make_engine(target))
    with maker() as session:
        assert st.get_setting(session, "bambuddy_api_key") == API_KEY
        assert auth.has_user(session)


def test_an_encrypted_backup_asks_for_the_password_and_never_shows_it(tmp_path, source, target):
    archive = backup_of(source, tmp_path, SECRET_BACKUP_PASSWORD)
    code, said, asked = run(archive, target, passwords=[SECRET_BACKUP_PASSWORD])
    assert code == 0, said.text()
    assert asked == [1] and cap_of(target) == 7
    assert SECRET_BACKUP_PASSWORD not in said.text()
    assert "encrypted" in said.text()


def test_a_wrong_password_may_be_tried_again_and_then_the_right_one_works(tmp_path, source, target):
    archive = backup_of(source, tmp_path, SECRET_BACKUP_PASSWORD)
    code, said, asked = run(archive, target, passwords=["wrong-one-1", SECRET_BACKUP_PASSWORD])
    assert code == 0 and len(asked) == 2 and cap_of(target) == 7
    assert "Wrong password" in said.text()


def test_three_wrong_passwords_end_it_and_change_nothing(tmp_path, source, target):
    archive = backup_of(source, tmp_path, SECRET_BACKUP_PASSWORD)
    code, said, asked = run(archive, target, passwords=["nope-1", "nope-2", "nope-3", SECRET_BACKUP_PASSWORD])
    assert code == restorecmd.EXIT_REFUSED and len(asked) == 3
    assert cap_of(target) == 3
    assert "Nothing was changed" in said.text()
    assert not (target.parent / "backups").exists()
    nothing_left_behind(target)


def test_an_empty_answer_for_the_password_counts_as_a_missing_password(tmp_path, source, target):
    archive = backup_of(source, tmp_path, SECRET_BACKUP_PASSWORD)
    code, said, asked = run(archive, target, passwords=["", "", ""])
    assert code == restorecmd.EXIT_REFUSED and cap_of(target) == 3
    assert "needs its password" in said.text()


def test_an_older_backup_is_restored_as_it_is_and_the_start_updates_it(tmp_path, target):
    older = known_revisions()[-2]
    old_source = fill(make_db(tmp_path / "old" / "reprintbuddy.db", revision=older), cap=9)
    archive = backup_of(old_source, tmp_path)
    code, said, _ = run(archive, target)
    assert code == 0, said.text()
    assert "older version" in said.text() and "brought up to date" in said.text()
    con = sqlite3.connect(target)
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchall() == [(older,)]
    finally:
        con.close()
    assert cap_of(target) == 9


# --- the checks are the interface's checks -----------------------------------------------------------


def test_a_file_that_is_no_backup_is_refused(tmp_path, target):
    stranger = tmp_path / "stranger.zip"
    with zipfile.ZipFile(stranger, "w") as archive:
        archive.writestr("hello.txt", "hi")
    code, said, _ = run(stranger, target)
    assert code == restorecmd.EXIT_REFUSED and cap_of(target) == 3
    assert "not a Reprintbuddy backup" in said.text() and "Nothing was changed" in said.text()


def test_a_text_file_is_refused(tmp_path, target):
    text = tmp_path / "notes.zip"
    text.write_text("this is not a zip file")
    code, _, _ = run(text, target)
    assert code == restorecmd.EXIT_REFUSED and cap_of(target) == 3


def test_a_backup_of_a_newer_version_is_refused(tmp_path, source, target):
    newer = rewritten(
        backup_of(source, tmp_path), tmp_path / "newer.zip", manifest=lambda m: m.update(app_version="99.0.0")
    )
    code, said, _ = run(newer, target)
    assert code == restorecmd.EXIT_REFUSED and cap_of(target) == 3
    assert "newer version" in said.text()


def test_a_damaged_database_inside_is_refused_and_leaves_no_candidate_behind(tmp_path, source, target):
    good = backup_of(source, tmp_path)
    with zipfile.ZipFile(good) as archive:
        cut = archive.read("database.sqlite")[:200]
    broken = rewritten(good, tmp_path / "broken.zip", **{"database.sqlite": cut})
    code, _, _ = run(broken, target)
    assert code == restorecmd.EXIT_REFUSED and cap_of(target) == 3
    nothing_left_behind(target)


def test_a_missing_file_is_a_clear_mistake(tmp_path, target):
    code, said, _ = run(tmp_path / "not-there.zip", target)
    assert code == restorecmd.EXIT_NOT_DONE and "file not found" in said.text()


# --- the service must not run ------------------------------------------------------------------------


def test_it_refuses_while_systemd_says_the_service_is_active(tmp_path, source, target):
    runner = FakeRunner()
    runner.on(IS_ACTIVE, CommandResult(0, "active\n"))
    archive = backup_of(source, tmp_path, SECRET_BACKUP_PASSWORD)
    code, said, asked = run(archive, target, runner=runner, probe=None, passwords=[SECRET_BACKUP_PASSWORD])
    assert code == restorecmd.EXIT_NOT_DONE
    assert asked == []  # the password is not even asked
    assert cap_of(target) == 3
    text = said.text()
    assert "systemctl stop reprintbuddy" in text
    assert "Another program on the same port" in text


@pytest.mark.parametrize("state", ["activating", "reloading", "deactivating"])
def test_other_living_states_count_as_running(tmp_path, source, target, state):
    runner = FakeRunner()
    runner.on(IS_ACTIVE, CommandResult(0, state + "\n"))
    code, _, _ = run(backup_of(source, tmp_path), target, runner=runner, probe=None)
    assert code == restorecmd.EXIT_NOT_DONE and cap_of(target) == 3


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize("state", ["inactive", "failed", "unknown"])
def test_a_stopped_unit_does_not_stand_in_the_way(tmp_path, source, target, state):
    runner = FakeRunner()
    runner.on(IS_ACTIVE, CommandResult(3, state + "\n"))
    with make_sessionmaker(make_engine(target))() as session:
        st.set_setting(session, "port", _free_port())
        st.set_setting(session, "host", "127.0.0.1")
    code, said, _ = run(backup_of(source, tmp_path), target, runner=runner, probe=None)
    assert code == 0, said.text()


def test_without_systemd_the_port_decides(tmp_path, source, target):
    runner = FakeRunner()
    runner.on(IS_ACTIVE, CommandResult(127, "", "not found"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        port = listener.getsockname()[1]
        with make_sessionmaker(make_engine(target))() as session:
            st.set_setting(session, "port", port)
            st.set_setting(session, "host", "127.0.0.1")
        code, said, _ = run(backup_of(source, tmp_path), target, runner=runner, probe=None)
        assert code == restorecmd.EXIT_NOT_DONE and cap_of(target) == 3
        assert f"port {port}" in said.text() and "systemctl stop reprintbuddy" in said.text()
    finally:
        listener.close()


def test_the_address_comes_from_the_database_or_the_defaults(tmp_path, target):
    with make_sessionmaker(make_engine(target))() as session:
        st.set_setting(session, "port", 9123)
        st.set_setting(session, "host", "192.0.2.10")
    assert restorecmd.listening_address(target) == ("192.0.2.10", 9123)
    assert restorecmd.listening_address(tmp_path / "none.db") == ("0.0.0.0", 8080)
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"x" * 5000)
    assert restorecmd.listening_address(garbage) == ("0.0.0.0", 8080)


def test_a_probe_that_cannot_connect_says_nothing(target):
    def no_route(*args, **kwargs):
        raise OSError("network is unreachable")

    assert restorecmd.service_running(target, FakeRunner(), connect=no_route) is None


# --- the database in place is gone or broken ---------------------------------------------------------------


def test_without_a_database_in_place_the_backup_becomes_the_database(tmp_path, source):
    missing = tmp_path / "fresh" / "reprintbuddy.db"
    code, said, _ = run(backup_of(source, tmp_path), missing)
    assert code == 0, said.text()
    assert cap_of(missing) == 7
    assert "There was no database to save" in said.text()


def test_a_broken_database_in_place_is_set_aside_and_replaced(tmp_path, source):
    broken = tmp_path / "data" / "reprintbuddy.db"
    broken.parent.mkdir()
    broken.write_bytes(b"this is not a database" * 400)
    (tmp_path / "data" / "reprintbuddy.db-wal").write_bytes(b"leftover")
    code, said, _ = run(backup_of(source, tmp_path), broken)
    assert code == 0, said.text()
    assert cap_of(broken) == 7
    aside = list((broken.parent / "backups").glob("damaged-*.db"))
    assert len(aside) == 1 and aside[0].read_bytes().startswith(b"this is not a database")
    assert not (tmp_path / "data" / "reprintbuddy.db-wal").exists()  # the new database starts clean
    assert str(aside[0]) in said.text()


def test_a_failing_install_into_a_broken_place_puts_the_old_file_back(tmp_path, source, monkeypatch):
    broken = tmp_path / "data" / "reprintbuddy.db"
    broken.parent.mkdir()
    broken.write_bytes(b"this is not a database" * 400)

    def fails(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(restorecmd, "_copy", fails)
    code, said, _ = run(backup_of(source, tmp_path), broken)
    assert code == restorecmd.EXIT_REFUSED
    assert broken.read_bytes().startswith(b"this is not a database")  # as it was
    nothing_left_behind(broken)


# --- the command line ------------------------------------------------------------------------------------------


def test_the_command_asks_the_password_on_the_terminal_only(tmp_path, source, target, monkeypatch, capsys):
    archive = backup_of(source, tmp_path, SECRET_BACKUP_PASSWORD)
    prompts = []

    def fake_getpass(prompt=""):
        prompts.append(prompt)
        return SECRET_BACKUP_PASSWORD

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    monkeypatch.setattr("reprintbuddy.cli.subprocess_runner", FakeRunner())
    monkeypatch.setattr(
        "reprintbuddy.ops.restorecmd.service_running", lambda *args, **kwargs: None
    )  # no real port in a test
    assert main(["restore-backup", str(archive), "--db", str(target)]) == 0
    output = capsys.readouterr()
    assert prompts and "assword" in prompts[0]
    assert SECRET_BACKUP_PASSWORD not in output.out + output.err
    assert "Restored the backup" in output.out
    assert cap_of(target) == 7


def test_the_command_has_no_password_argument():
    with pytest.raises(SystemExit):
        main(["restore-backup", "x.zip", "--password", "abc"])
    with pytest.raises(SystemExit):
        main(["restore-backup", "x.zip", "abc"])


def test_the_command_reports_a_running_service_with_exit_code_two(tmp_path, source, target, monkeypatch, capsys):
    runner = FakeRunner()
    runner.on(IS_ACTIVE, CommandResult(0, "active\n"))
    monkeypatch.setattr("reprintbuddy.cli.subprocess_runner", runner)
    assert main(["restore-backup", str(backup_of(source, tmp_path)), "--db", str(target)]) == 2
    assert "systemctl stop reprintbuddy" in capsys.readouterr().err
    assert cap_of(target) == 3


def test_a_refusal_goes_to_the_error_stream(tmp_path, target, monkeypatch, capsys):
    monkeypatch.setattr("reprintbuddy.cli.subprocess_runner", FakeRunner())
    assert main(["restore-backup", str(tmp_path / "missing.zip"), "--db", str(target)]) == 2
    output = capsys.readouterr()
    assert "file not found" in output.err
    assert "file not found" not in output.out


# --- found by the security review of p100 -----------------------------------------------------------------


def test_a_running_update_unit_also_stands_in_the_way(tmp_path, source, target):
    """An update that rolls back stops the service and puts its own copy of the database back."""
    runner = FakeRunner()
    runner.on((SYSTEMCTL, "is-active", UPDATE_SERVICE), CommandResult(0, "activating\n"))
    code, said, asked = run(backup_of(source, tmp_path), target, runner=runner, probe=None)
    assert code == restorecmd.EXIT_NOT_DONE and cap_of(target) == 3
    assert UPDATE_SERVICE in said.text()


def test_a_service_started_while_the_password_was_typed_is_noticed(tmp_path, source, target):
    answers = iter([None, "something answers on port 8080"])
    code, said, asked = run(
        backup_of(source, tmp_path, SECRET_BACKUP_PASSWORD),
        target,
        passwords=[SECRET_BACKUP_PASSWORD],
        probe=lambda: next(answers),
    )
    assert code == restorecmd.EXIT_NOT_DONE and asked == [1]
    assert "started meanwhile" in said.text() and cap_of(target) == 3
    nothing_left_behind(target)


def test_ctrl_c_while_checking_leaves_no_decrypted_copy(tmp_path, source, target, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(fb, "verify_database", interrupted)
    code, said, _ = run(backup_of(source, tmp_path), target)
    assert code == restorecmd.EXIT_NOT_DONE and "interrupted" in said.text()
    assert "Nothing was changed" in said.text() and cap_of(target) == 3
    nothing_left_behind(target)


def test_ctrl_c_while_decrypting_leaves_no_decrypted_copy(tmp_path, source, target, monkeypatch):
    """The sister of the cancelled check of the page (v0.3.11 p140): the interrupt comes in the middle of
    writing the decrypted file, not after it, and the file exists at that moment."""

    def interrupted_after_a_block(archive, sealing, key, aad, handle, *rest):
        handle.write(b"a block of the decrypted database")
        handle.flush()
        raise KeyboardInterrupt

    monkeypatch.setattr(fb, "_open_into", interrupted_after_a_block)
    code, said, _ = run(backup_of(source, tmp_path, SECRET_BACKUP_PASSWORD), target, passwords=[SECRET_BACKUP_PASSWORD])
    assert code == restorecmd.EXIT_NOT_DONE and "interrupted" in said.text()
    nothing_left_behind(target)


def test_a_relative_path_gets_a_hint_about_the_full_path(tmp_path, target):
    code, said, _ = run(Path("backup.zip"), target)
    assert code == restorecmd.EXIT_NOT_DONE and "full path" in said.text()


@pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="file modes are a POSIX matter")
def test_what_the_command_creates_is_private_whatever_the_umask(tmp_path, source, monkeypatch):
    import os

    previous = os.umask(0o022)
    try:
        broken = tmp_path / "data" / "reprintbuddy.db"
        broken.parent.mkdir()
        broken.write_bytes(b"this is not a database" * 400)
        code, said, _ = run(backup_of(source, tmp_path), broken)
        assert code == 0, said.text()
        assert (broken.stat().st_mode & 0o777) == 0o600
        assert ((broken.parent / "backups").stat().st_mode & 0o777) == 0o700
        assert os.umask(0o022) == 0o022  # the shell's umask is back after the command
    finally:
        os.umask(previous)


def test_a_move_that_fails_half_way_puts_the_old_files_back(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "reprintbuddy.db"
    db_path.parent.mkdir()
    db_path.write_bytes(b"old main file")
    Path(f"{db_path}-wal").write_bytes(b"old wal")
    real = Path.replace

    def failing(self, target):
        if str(self).endswith("-wal") and "damaged" in str(target):
            raise PermissionError("no")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", failing)
    with pytest.raises(PermissionError):
        restorecmd._install_unreadable(tmp_path / "unused.db", db_path, NOW)
    assert db_path.read_bytes() == b"old main file"  # the main file is back where it was
    assert Path(f"{db_path}-wal").read_bytes() == b"old wal"


def test_when_even_the_way_back_fails_the_message_says_where_the_file_is(tmp_path, source, monkeypatch):
    broken = tmp_path / "data" / "reprintbuddy.db"
    broken.parent.mkdir()
    broken.write_bytes(b"this is not a database" * 400)
    real = Path.replace

    def failing(self, target):
        if "damaged" in str(self) and str(target) == str(broken):  # moving back
            raise PermissionError("no")
        return real(self, target)

    def copy_fails(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", failing)
    monkeypatch.setattr(restorecmd, "_copy", copy_fails)
    code, said, _ = run(backup_of(source, tmp_path), broken)
    assert code == restorecmd.EXIT_REFUSED
    text = said.text()
    assert "could not be put back" in text and "damaged-" in text and "Nothing was changed" not in text


def test_the_marks_of_the_log_of_the_replaced_database_are_carried_over(tmp_path, source, target):
    """p186: the restore from the command line leaves the marks of the database it replaces for the next start,
    so a state the log already wrote does not come back from an older backup."""
    from reprintbuddy.activity.writer import read_restored_marker

    for path, state in ((source, "ok"), (target, "unreachable")):
        con = sqlite3.connect(path)
        with con:
            con.execute(
                "INSERT INTO activity_marks (key, state, at) VALUES ('bambuddy.connection', ?, '2026-11-08 09:00:00')",
                (state,),
            )
        con.close()
    code, said, _ = run(backup_of(source, tmp_path), target)
    assert code == 0, said.text()
    marker = read_restored_marker(target.parent)
    assert marker["via"] == "cli"
    assert marker["marks"] == {"bambuddy.connection": {"state": "unreachable", "at": "2026-11-08T09:00:00"}}


def test_the_marks_of_the_push_messages_of_the_replaced_database_are_carried_over(tmp_path, source, target):
    """p220: the restore from the command line also leaves the marks of the push messages (`notify_marks`) of the
    database it replaces for the next start, so an event already reported does not go to the phone again."""
    from reprintbuddy.activity.writer import read_restored_marker

    con = sqlite3.connect(target)
    with con:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ha_enabled', 'true')")  # messages on
        con.execute(
            "INSERT INTO notify_marks (kind, ref, state, streak, first_seen_at, notified_at)"
            " VALUES ('print_done', '17', 'sent', 0, '2026-11-08 08:00:00', '2026-11-08 08:01:00')"
        )
    con.close()
    code, said, _ = run(backup_of(source, tmp_path), target)
    assert code == 0, said.text()
    marker = read_restored_marker(target.parent)
    assert marker["notify_marks"] == [
        {
            "kind": "print_done",
            "ref": "17",
            "state": "sent",
            "streak": 0,
            "first_seen_at": "2026-11-08T08:00:00",
            "notified_at": "2026-11-08T08:01:00",
            "gone_since": None,
        }
    ]


def test_marks_only_in_the_wal_of_a_crashed_service_are_carried_over_too(tmp_path, source, target):
    """p221: after a crash the database in place may have its last writes only in the -wal file, without -shm.
    The marks of the log and of the push messages written there must reach the restore marker all the same.
    (Root checked for v0.3.15: `_readable` opens the database with write rights before the marks are read, which
    recovers the -wal into the database; the read-only read that follows finds no leftovers.)"""
    import shutil

    from reprintbuddy.activity.writer import read_restored_marker

    con = sqlite3.connect(target)
    con.execute("PRAGMA wal_autocheckpoint=0")
    with con:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ha_enabled', 'true')")  # messages on
        con.execute(
            "INSERT INTO activity_marks (key, state, at)"
            " VALUES ('bambuddy.connection', 'unreachable', '2026-11-08 09:00:00')"
        )
        con.execute(
            "INSERT INTO notify_marks (kind, ref, state, streak, first_seen_at)"
            " VALUES ('below_min', '4', 'sent', 0, '2026-11-08 08:00:00')"
        )
    crashed = tmp_path / "crashed" / "reprintbuddy.db"
    crashed.parent.mkdir()
    for suffix in ("", "-wal"):  # as a killed process leaves it: the writes only in the -wal, no -shm
        shutil.copy(f"{target}{suffix}", f"{crashed}{suffix}")
    con.close()
    assert Path(f"{crashed}-wal").stat().st_size > 0 and not Path(f"{crashed}-shm").exists()

    code, said, _ = run(backup_of(source, tmp_path), crashed)
    assert code == 0, said.text()
    marker = read_restored_marker(crashed.parent)
    assert marker["marks"] == {"bambuddy.connection": {"state": "unreachable", "at": "2026-11-08T09:00:00"}}
    assert [(m["kind"], m["ref"], m["state"]) for m in marker["notify_marks"]] == [("below_min", "4", "sent")]


def test_it_refuses_while_an_update_holds_the_lock(tmp_path, source, target):
    """p91: an update started by hand runs outside the unit, so systemd says nothing; the lock does."""
    from reprintbuddy.ops import oplock

    held = oplock.acquire(target.parent, "update")
    try:
        code, said, asked = run(backup_of(source, tmp_path), target)
    finally:
        held.release()
    assert code == restorecmd.EXIT_NOT_DONE and asked == []
    assert cap_of(target) == 3
    assert "an update is running" in said.text() and "Nothing was changed." in said.text()


def test_it_holds_the_lock_while_it_replaces_and_lets_it_go(tmp_path, source, target, monkeypatch):
    from reprintbuddy.ops import oplock

    seen = []
    real = fb.replace_database

    def spy(*args, **kwargs):
        seen.append(oplock.holder(target.parent))
        return real(*args, **kwargs)

    monkeypatch.setattr(fb, "replace_database", spy)
    code, _, _ = run(backup_of(source, tmp_path), target)
    assert code == restorecmd.EXIT_OK and seen == ["restore"]
    assert oplock.holder(target.parent) is None
