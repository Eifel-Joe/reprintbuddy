"""p531 (v1.2.0-beta.2): the same restore through the page and through the command line.

Restoring a full backup (Spec 11.6) has two ways in: the page on the settings, and the emergency command
`reprintbuddy restore-backup` for when the page cannot be reached. Both run the same checks and the same
replace, each with code of its own around them. These tests give both ways the same input and expect the
same outcome where the outcome is meant to be the same: what is in the database afterwards, the backup made
before, what is left in the work folder, the note for the activity log. Where the ways differ on purpose
(the page keeps the upload for another try, the command line says "nothing was changed"), the test says so.

They pin the behaviour before the two ways share the end of the restore and must stay green, unchanged,
after it. Cancelling the replace itself is left to the tests of each way (`test_backup_abandon.py`,
`tests/ops/test_restorecmd.py`): there the two ways differ on purpose (restart gate, Ctrl-C message).
"""

from __future__ import annotations

import asyncio
import errno
import io
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import PASSWORD, FakeRunner, make_client

from reprintbuddy.activity.writer import RESTORED_MARKER, current_marks
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.ops import restorecmd
from reprintbuddy.ops.status import UpdateStatus, write_status

from test_backup_abandon import Held, async_client, cancel_and_let_go, settled, token_of, upload_token
from test_ops_full_backup import MAINTENANCE, confirm, made_backup, set_queue_cap, upload, work_files, zip_members

BACKUP_PASSWORD = "backup-password-1"
WAYS = ("page", "command")
SECRET_PREFIXES = ("candidate-", "rebuilt-", "trial-")  # what a restore must never leave in the work folder
LEFT_OUT = {"sessions"}  # emptied in every backup and again before the replace

pytestmark = pytest.mark.usefixtures("fast_kdf")  # backups derive their key cheaply (conftest)

ENGLISH = json.loads(
    (Path(__file__).resolve().parents[2] / "src" / "reprintbuddy" / "locales" / "en.json").read_text(encoding="utf-8")
)


@dataclass
class Outcome:
    result: str  # "restored", a refusal code ("foreign", "wrong_password", "failed", ...), "retry", "interrupted"
    text: str


# --- the two ways ---------------------------------------------------------------------------------


def by_page(env, content: bytes, password: str = "") -> Outcome:
    response = upload(env, content)
    if response.headers.get("location", "").startswith(MAINTENANCE):
        return _page_outcome(response)
    return _page_outcome(confirm(env, password))


def _page_outcome(response) -> Outcome:
    if response.status_code == 303:
        found = re.search(r"ops=restore_([a-z_]+)#", response.headers["location"])
        assert found, response.headers["location"]
        return Outcome(found.group(1), response.headers["location"])
    assert response.status_code == 200, response.status_code
    if "Sign in with the user name and password of the backup" in response.text:
        return Outcome("restored", response.text)
    for key in ("wrong_password", "password_needed", "retry"):
        if ENGLISH[f"ops.restore.{key}"] in response.text:
            return Outcome(key, response.text)
    raise AssertionError("an answer of the page these tests do not know")


def by_command(env, content: bytes, password: str = "") -> Outcome:
    archive = env.db_path.parent / "the-backup.zip"
    archive.write_bytes(content)
    said: list[str] = []
    code = restorecmd.restore_backup(
        archive,
        env.db_path,
        runner=FakeRunner(),
        ask_password=lambda: password,
        say=said.append,
        now=datetime.now(UTC),
        probe=lambda: None,  # the service is stopped
    )
    archive.unlink()
    text = "\n".join(said)
    if code == restorecmd.EXIT_OK:
        return Outcome("restored", text)
    for key, message in restorecmd.MESSAGES.items():
        if message in text:
            return Outcome(key, text)
    if "the check failed" in text or "the restore failed" in text:
        return Outcome("failed", text)
    if "interrupted" in text:
        return Outcome("interrupted", text)
    raise AssertionError(f"an answer of the command line these tests do not know: {text}")


def restore(way: str, env, content: bytes, password: str = "") -> Outcome:
    return (by_page if way == "page" else by_command)(env, content, password)


# --- what is there ----------------------------------------------------------------------------------


def rows(path: Path, *, leave_out=LEFT_OUT) -> dict[str, list[tuple]]:
    """Every row of every table, in a fixed order; read only."""
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [
            name
            for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            if not name.startswith("sqlite_") and name not in leave_out
        ]
        return {name: sorted(con.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr) for name in tables}
    finally:
        con.close()


def database_of(content: bytes, password: str, folder: Path) -> Path:
    """The database inside a backup, as the backup holds it."""
    folder.mkdir(exist_ok=True)
    archive = folder / "inside.zip"
    archive.write_bytes(content)
    target = folder / "inside.db"
    fb.extract_database(archive, fb.read_manifest(archive), password or None, target)
    return target


def before_restore_backups(env) -> list[Path]:
    folder = env.db_path.parent / "backups"
    return sorted(folder.glob("reprintbuddy-before-restore-*.db")) if folder.is_dir() else []


def secrets_left(env) -> list[str]:
    return [name for name in work_files(env) if name.startswith(SECRET_PREFIXES)]


def marker(env) -> dict | None:
    path = env.db_path.parent / RESTORED_MARKER
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def changed_since(env) -> dict:
    """Change the running database after the backup was made, so that "old" and "backup" differ."""
    set_queue_cap(env, 9)
    return rows(env.db_path)


# --- the same backup, both ways ------------------------------------------------------------------------


@pytest.mark.parametrize("password", ["", BACKUP_PASSWORD], ids=["plain", "encrypted"])
@pytest.mark.parametrize("way", WAYS)
def test_a_good_backup_ends_in_the_same_state_both_ways(ops_env, tmp_path, way, password):
    content = made_backup(ops_env, password)
    expected = rows(database_of(content, password, tmp_path / "expected"))
    old = changed_since(ops_env)
    old_marks = current_marks(ops_env.db_path)
    manifest = fb.read_manifest(_saved(content, tmp_path))

    outcome = restore(way, ops_env, content, password)

    assert outcome.result == "restored", outcome.text
    # the database holds exactly the rows of the backup (the browsers signed in are gone)
    assert rows(ops_env.db_path) == expected
    assert rows(ops_env.db_path, leave_out=set())["sessions"] == []
    # one backup of what was there before, with exactly that content
    made = before_restore_backups(ops_env)
    assert len(made) == 1
    assert rows(made[0]) == old
    # the note the next start logs the restore from
    note = marker(ops_env)
    assert note is not None
    assert datetime.fromisoformat(note["created_at"]) == manifest.created_at
    assert note["version"] == manifest.app_version
    assert note["via"] == (None if way == "page" else "cli")
    assert note["marks"] == old_marks
    assert secrets_left(ops_env) == []


# Today only the page keeps the backup a failed rollback names; whether the command line does too waits for
# a decision (p531, finding A4). The case stays a list of ways so that it can grow by one.
@pytest.mark.parametrize("way", ["page"])
def test_the_backup_a_failed_rollback_names_survives_the_pruning(ops_env, way):
    content = made_backup(ops_env)
    folder = ops_env.db_path.parent / "backups"
    folder.mkdir(exist_ok=True)
    older = [folder / f"reprintbuddy-v1.0.{n}-2026010{n}T080000Z.db" for n in range(1, 7)]  # oldest first
    for path in older:
        path.write_bytes(b"an old backup")
    named = older[0]
    write_status(
        ops_env.db_path.parent,
        UpdateStatus(state="rollback_failed", step="rollback", backup=str(named), started_at=datetime.now(UTC)),
    )

    assert restore(way, ops_env, content).result == "restored"

    # six old ones and the new one, five are kept: the oldest is pruned, except the one the rollback names
    assert named.exists()
    assert not older[1].exists()
    assert len(before_restore_backups(ops_env)) == 1


def test_the_page_never_asks_whether_the_database_could_be_set_aside(ops_env, monkeypatch):
    """Only the command line, with the service stopped, may set a broken database aside. The page restores
    into the database its own service holds open: a check that failed there for a moment would move the
    live database away (p531, `restore.install`)."""
    from reprintbuddy.ops import restore as shared

    def never(path):
        raise AssertionError("the page asked whether to set the database aside")

    monkeypatch.setattr(shared, "database_readable", never)
    content = made_backup(ops_env)

    assert by_page(ops_env, content).result == "restored"
    assert list((ops_env.db_path.parent / "backups").glob("damaged-*")) == []


def _saved(content: bytes, tmp_path: Path) -> Path:
    path = tmp_path / "saved.zip"
    path.write_bytes(content)
    return path


# --- refused: the same reason, nothing changed --------------------------------------------------------


def _damaged(env) -> tuple[bytes, str]:
    members = zip_members(made_backup(env, BACKUP_PASSWORD))
    sealed = bytearray(members["database.sqlite.enc"])
    sealed[100] ^= 1
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", members["manifest.json"])
        archive.writestr("database.sqlite.enc", bytes(sealed))
    return buffer.getvalue(), BACKUP_PASSWORD


def _incompatible(env) -> tuple[bytes, str]:
    folder = env.db_path.parent / "other"
    folder.mkdir()
    other = make_client(folder / "other.db")
    other.app.state.engine.dispose()
    con = sqlite3.connect(folder / "other.db")
    con.execute("CREATE VIEW sneaky AS SELECT * FROM settings")
    con.commit()
    con.close()
    made = fb.create_backup(folder / "other.db", work_dir=folder / "w", password=None, now=datetime.now(UTC))
    return made.path.read_bytes(), ""


CASES = {
    "foreign": lambda env, mp: (b"not a zip at all", ""),
    "too_new": lambda env, mp: (made_backup(env), mp.setattr(fb, "__version__", "0.0.1") or ""),
    "damaged": lambda env, mp: _damaged(env),
    "incompatible": lambda env, mp: _incompatible(env),
    "wrong_password": lambda env, mp: (made_backup(env, BACKUP_PASSWORD), "not-the-password-1"),
    "password_needed": lambda env, mp: (made_backup(env, BACKUP_PASSWORD), ""),
}


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("way", WAYS)
def test_a_refused_backup_is_refused_for_the_same_reason_and_changes_nothing(ops_env, monkeypatch, way, case):
    content, password = CASES[case](ops_env, monkeypatch)
    old = changed_since(ops_env)

    outcome = restore(way, ops_env, content, password)

    assert outcome.result == case, outcome.text
    # the page logs a refusal into the activity log (Spec 10.7); the command line does not (p660)
    assert rows(ops_env.db_path, leave_out=LEFT_OUT | {"activity"}) == {
        name: value for name, value in old.items() if name != "activity"
    }
    assert before_restore_backups(ops_env) == []
    assert marker(ops_env) is None
    assert secrets_left(ops_env) == []


# --- broken off in the middle ------------------------------------------------------------------------


@pytest.mark.parametrize("way", WAYS)
def test_a_copy_that_fails_after_the_backup_was_made_changes_nothing(ops_env, monkeypatch, way):
    """The one-step copy into the running database fails (a full disk), after the backup of the old one."""
    content = made_backup(ops_env)
    old = changed_since(ops_env)

    def full_disk(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(fb, "restore_database", full_disk)
    outcome = restore(way, ops_env, content)

    # on purpose: the page keeps the upload and asks to try again (p141), the command line says what failed
    assert outcome.result == {"page": "retry", "command": "failed"}[way], outcome.text
    if way == "command":
        assert "Nothing was changed." in outcome.text
    assert rows(ops_env.db_path) == old
    made = before_restore_backups(ops_env)  # it was made before the copy failed; it stays
    assert len(made) == 1 and rows(made[0]) == old
    assert marker(ops_env) is None
    assert secrets_left(ops_env) == []


@pytest.mark.parametrize("way", WAYS)
def test_an_unexpected_error_in_the_check_is_a_refusal_and_changes_nothing(ops_env, monkeypatch, way):
    content = made_backup(ops_env)
    old = changed_since(ops_env)

    def odd(*args, **kwargs):
        raise RuntimeError("nobody expected this")

    monkeypatch.setattr(fb, "verify_database", odd)
    outcome = restore(way, ops_env, content)

    assert outcome.result == "failed", outcome.text
    assert rows(ops_env.db_path, leave_out=LEFT_OUT | {"activity"}) == {
        name: value for name, value in old.items() if name != "activity"
    }
    assert before_restore_backups(ops_env) == []
    assert marker(ops_env) is None
    assert secrets_left(ops_env) == []


def test_ctrl_c_on_the_command_line_while_it_checks_changes_nothing(ops_env, monkeypatch):
    content = made_backup(ops_env, BACKUP_PASSWORD)
    old = changed_since(ops_env)
    real = fb._copy_rows

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(fb, "_copy_rows", interrupted)
    outcome = by_command(ops_env, content, BACKUP_PASSWORD)

    assert outcome.result == "interrupted", outcome.text
    assert "Nothing was changed." in outcome.text
    assert rows(ops_env.db_path) == old
    assert before_restore_backups(ops_env) == []
    assert marker(ops_env) is None
    assert secrets_left(ops_env) == []


# Serial part of the CI (tests/test_ci_split.py): the helpers of test_backup_abandon.py work with fixed
# pauses (0.2 s before letting the worker go, 0.4 s of quiet) and a deadline of 10 s.
@pytest.mark.serial
async def test_a_page_closed_while_it_checks_changes_nothing(ops_env, monkeypatch):
    """The sister of the Ctrl-C above: the browser goes away while the rows are rebuilt."""
    content = made_backup(ops_env, BACKUP_PASSWORD)
    old = changed_since(ops_env)
    upload(ops_env, content)
    held = Held(monkeypatch, fb, "_copy_rows")
    async with async_client(ops_env) as ac:
        data = {
            "csrf_token": await token_of(ac),
            "backup_password": BACKUP_PASSWORD,
            "upload_token": upload_token(ops_env),
            "login_password": PASSWORD,
        }
        task = asyncio.create_task(ac.post("/settings/restore", data=data))
        await held_entered(held)
        await cancel_and_let_go(task, held)
    assert await settled(ops_env) == []
    assert rows(ops_env.db_path) == old
    assert before_restore_backups(ops_env) == []
    assert marker(ops_env) is None
    assert ops_env.client.app.state.restore.phase == "idle"


async def held_entered(held: Held) -> None:
    assert await asyncio.to_thread(held.entered.wait, 20)
