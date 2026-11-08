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
from reprintbuddy.ops.service import SERVICE, SYSTEMCTL

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
