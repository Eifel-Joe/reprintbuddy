"""Full backup and restore on the settings page (Spec 11.6)."""

import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ADMIN, PASSWORD, csrf_of, make_client

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.ops import backupcrypto as bc
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.service import RESTART_CMD, UPDATE_SERVICE
from reprintbuddy.ops.status import UpdateStatus, write_status

FAST = bc.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)
BACKUP_PASSWORD = "backup-password-1"
MAINTENANCE = "/settings?tab=maintenance"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(fb, "KDF", FAST)


def post(env, path, **data):
    token = csrf_of(env.client, "/settings")
    return env.client.post(path, data={"csrf_token": token, **data})


def download(env, password="", repeat=None, login=PASSWORD):
    return post(
        env,
        "/settings/backup",
        backup_password=password,
        backup_password_repeat=password if repeat is None else repeat,
        login_password=login,
    )


def upload(env, content: bytes, name="backup.zip"):
    token = csrf_of(env.client, "/settings")
    return env.client.post(
        "/settings/restore/upload",
        data={"csrf_token": token},
        files={"backup_file": (name, content, "application/zip")},
    )


def confirm(env, password="", login=PASSWORD):
    """Press the button of the confirmation page (with the token of the upload it shows)."""
    import re

    page = env.client.get("/settings/restore")
    found = re.search(r'name="upload_token" value="([^"]*)"', page.text) if page.status_code == 200 else None
    token = csrf_of(env.client, "/settings")
    return env.client.post(
        "/settings/restore",
        data={
            "csrf_token": token,
            "backup_password": password,
            "upload_token": found.group(1) if found else "",
            "login_password": login,
        },
    )


def queue_cap(env):
    with env.client.app.state.sessionmaker() as session:
        return st.get_setting(session, "queue_cap")


def set_queue_cap(env, value):
    with env.client.app.state.sessionmaker() as session:
        st.set_setting(session, "queue_cap", value)


def work_files(env):
    work = fb.work_dir_for(env.db_path)
    return sorted(p.name for p in work.iterdir()) if work.is_dir() else []


def backups(env):
    folder = env.db_path.parent / "backups"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def zip_members(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def made_backup(env, password="") -> bytes:
    response = download(env, password)
    assert response.status_code == 200, response.text
    return response.content


def mark_update_running(env):
    write_status(
        env.db_path.parent,
        UpdateStatus(state="running", step="install", started_at=datetime.now(UTC) - timedelta(seconds=5)),
    )
    env.runner.on(("/usr/bin/systemctl", "show"), CommandResult(0, "active\n"))


# --- the page --------------------------------------------------------------------------------


def test_maintenance_tab_offers_backup_restore_and_export(ops_env):
    page = ops_env.client.get(MAINTENANCE).text
    assert 'action="/settings/backup"' in page
    assert 'name="backup_password"' in page and 'name="backup_password_repeat"' in page
    assert 'action="/settings/restore/upload"' in page and 'enctype="multipart/form-data"' in page
    assert 'href="/settings/export"' in page  # the export without secrets stays (Spec 11.6)
    assert "Without a password, anyone who has the file gets the keys" in page
    assert 'autocomplete="new-password"' in page  # password managers must not fill in the login


# --- downloading -----------------------------------------------------------------------------


def test_download_without_password(ops_env):
    response = download(ops_env)
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="reprintbuddy-backup-')
    assert disposition.endswith('.zip"') and "encrypted" not in disposition
    content = zip_members(response.content)
    assert json.loads(content["manifest.json"])["encrypted"] is False
    assert b"bb_testkey" in content["database.sqlite"]  # the full backup holds the key on purpose
    assert work_files(ops_env) == []  # nothing left on the disk


def test_download_with_password(ops_env):
    response = download(ops_env, BACKUP_PASSWORD)
    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith('-encrypted.zip"')
    content = zip_members(response.content)
    assert set(content) == {"manifest.json", "database.sqlite.enc"}
    assert b"bb_testkey" not in content["database.sqlite.enc"]
    assert work_files(ops_env) == []


@pytest.mark.parametrize(
    ("password", "repeat", "code"),
    [
        (BACKUP_PASSWORD, "something-else-1", "backup_mismatch"),
        ("short", "short", "backup_length"),
        ("", "something", "backup_mismatch"),
    ],
)
def test_download_refuses_bad_passwords(ops_env, password, repeat, code):
    response = download(ops_env, password, repeat)
    assert response.status_code == 303
    assert response.headers["location"] == f"{MAINTENANCE}&ops={code}#ops-backup"
    page = ops_env.client.get(response.headers["location"]).text
    assert 'role="alert"' in page
    assert work_files(ops_env) == []


def test_download_is_no_get_route(ops_env):
    assert ops_env.client.get("/settings/backup").status_code in (404, 405)


def test_download_paused_during_update(ops_env):
    mark_update_running(ops_env)
    assert download(ops_env).status_code == 503


# --- restoring: the happy paths --------------------------------------------------------------


def test_restore_plain_backup(ops_env):
    content = made_backup(ops_env)
    set_queue_cap(ops_env, 9)
    with ops_env.client.app.state.sessionmaker() as session:
        auth.set_password(session, ADMIN, "a-newer-password-1")
    old_secret = ops_env.client.app.state.session_secret
    # set_password ended every session: sign in again with the new password
    token = csrf_of(ops_env.client, "/login")
    ops_env.client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": "a-newer-password-1"})

    response = upload(ops_env, content)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/restore"
    page = ops_env.client.get("/settings/restore").text
    assert "Not encrypted" in page
    assert 'name="backup_password"' not in page
    assert "Everybody is signed out" in page
    assert '<p class="page-sub">What the uploaded backup contains, before it replaces the current data.</p>' in page  # p72

    response = confirm(ops_env, login="a-newer-password-1")  # the password of the running database
    assert response.status_code == 200
    assert "Sign in with the user name and password of the backup" in response.text
    assert '<p class="page-sub">The data of the backup is in place; Reprintbuddy restarts with it.</p>' in response.text  # p72
    assert RESTART_CMD in ops_env.runner.calls
    assert queue_cap(ops_env) == 10  # the value of the backup (the default)
    assert [name for name in backups(ops_env) if "before-restore" in name]
    assert work_files(ops_env) == []
    # the browser is signed out: its session is not in the restored database
    assert ops_env.client.get("/settings").status_code in (302, 303)

    restarted = make_client(ops_env.db_path)  # what the service is after the restart
    assert restarted.app.state.session_secret == old_secret  # the backup's secret (same here)
    with restarted.app.state.sessionmaker() as session:
        assert auth.authenticate(session, ADMIN, PASSWORD) is not None
        assert auth.authenticate(session, ADMIN, "a-newer-password-1") is None


def test_restore_encrypted_backup_asks_for_the_password(ops_env):
    content = made_backup(ops_env, BACKUP_PASSWORD)
    set_queue_cap(ops_env, 9)
    upload(ops_env, content)
    page = ops_env.client.get("/settings/restore").text
    assert "Encrypted" in page
    assert 'name="backup_password"' in page

    response = confirm(ops_env, "not-the-password")
    assert response.status_code == 200
    assert "The password is wrong" in response.text
    assert queue_cap(ops_env) == 9 and backups(ops_env) == []
    assert ops_env.runner.calls == []

    response = confirm(ops_env)
    assert "This backup is encrypted. Enter its password." in response.text
    assert queue_cap(ops_env) == 9

    response = confirm(ops_env, BACKUP_PASSWORD)  # the upload is still there
    assert response.status_code == 200
    assert RESTART_CMD in ops_env.runner.calls
    assert queue_cap(ops_env) == 10


def test_restore_restores_the_other_secrets(ops_env, tmp_path):
    other = make_client(tmp_path / "other.db")
    with other.app.state.sessionmaker() as session:
        auth.create_admin(session, "someone", "someone-password-1")
        st.set_setting(session, "bambuddy_api_key", "bb_other_key")
    content = fb.create_backup(tmp_path / "other.db", work_dir=tmp_path / "w", password=None, now=datetime.now(UTC))
    upload(ops_env, content.path.read_bytes())
    confirm(ops_env)
    restarted = make_client(ops_env.db_path)
    assert restarted.app.state.session_secret == other.app.state.session_secret
    with restarted.app.state.sessionmaker() as session:
        assert st.get_setting(session, "bambuddy_api_key") == "bb_other_key"
        assert auth.authenticate(session, "someone", "someone-password-1") is not None


def test_engine_is_paused_while_the_database_is_replaced(ops_env, monkeypatch):
    content = made_backup(ops_env)
    upload(ops_env, content)
    seen = {}
    real = fb.replace_database
    app = ops_env.client.app

    def spy(*args, **kwargs):
        seen["paused"] = app.state.ticker.update_running()
        seen["locked"] = app.state.ticker.held()
        return real(*args, **kwargs)

    monkeypatch.setattr(fb, "replace_database", spy)
    confirm(ops_env)
    assert seen == {"paused": True, "locked": True}


def test_cancel_removes_the_upload(ops_env):
    upload(ops_env, made_backup(ops_env))
    assert work_files(ops_env)
    response = post(ops_env, "/settings/restore/cancel")
    assert response.status_code == 303
    assert work_files(ops_env) == []
    assert ops_env.client.get("/settings/restore").status_code == 303


def test_confirm_without_upload(ops_env):
    response = confirm(ops_env)
    assert response.status_code == 303
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_expired#ops-restore"


def test_a_new_upload_replaces_the_old_one(ops_env):
    content = made_backup(ops_env)
    upload(ops_env, content)
    upload(ops_env, content)
    assert len(work_files(ops_env)) == 1


def test_start_clears_leftovers(ops_env):
    work = fb.ensure_work_dir(fb.work_dir_for(ops_env.db_path))
    (work / "upload-old.zip").write_bytes(b"x")
    make_client(ops_env.db_path)
    assert work_files(ops_env) == []


# --- restoring: what is refused ----------------------------------------------------------------


def expect_refused(env, response, code):
    assert response.status_code == 303
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_{code}#ops-restore"
    assert 'role="alert"' in env.client.get(response.headers["location"]).text
    assert work_files(env) == []
    assert backups(env) == []
    assert env.runner.calls == []


def test_foreign_file(ops_env):
    expect_refused(ops_env, upload(ops_env, b"not a zip at all"), "foreign")


def test_too_new(ops_env, monkeypatch):
    content = made_backup(ops_env)
    monkeypatch.setattr(fb, "__version__", "0.0.1")  # this install is older than the backup
    expect_refused(ops_env, upload(ops_env, content), "too_new")


def test_too_large(ops_env, monkeypatch):
    monkeypatch.setattr(fb, "MAX_UPLOAD_BYTES", 1000)
    expect_refused(ops_env, upload(ops_env, made_backup(ops_env)), "too_large")


def test_upload_without_length(ops_env):
    token = csrf_of(ops_env.client, "/settings")
    body = (
        b"--x\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n" + token.encode() + b"\r\n--x--\r\n"
    )
    response = ops_env.client.post(
        "/settings/restore/upload",
        content=iter([body]),  # streamed: no Content-Length
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    expect_refused(ops_env, response, "too_large")


def test_damaged_file_is_refused_at_confirm(ops_env):
    content = zip_members(made_backup(ops_env, BACKUP_PASSWORD))
    sealed = bytearray(content["database.sqlite.enc"])
    sealed[100] ^= 1
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", content["manifest.json"])
        archive.writestr("database.sqlite.enc", bytes(sealed))
    set_queue_cap(ops_env, 9)
    assert upload(ops_env, buffer.getvalue()).status_code == 303
    expect_refused(ops_env, confirm(ops_env, BACKUP_PASSWORD), "damaged")
    assert queue_cap(ops_env) == 9


def test_incompatible_database_is_refused_at_confirm(ops_env, tmp_path):
    other = make_client(tmp_path / "other.db")
    other.app.state.engine.dispose()
    con = sqlite3.connect(tmp_path / "other.db")
    con.execute("CREATE VIEW sneaky AS SELECT * FROM settings")
    con.commit()
    con.close()
    content = fb.create_backup(tmp_path / "other.db", work_dir=tmp_path / "w", password=None, now=datetime.now(UTC))
    upload(ops_env, content.path.read_bytes())
    expect_refused(ops_env, confirm(ops_env), "incompatible")


def test_restore_paused_during_update(ops_env):
    content = made_backup(ops_env)
    mark_update_running(ops_env)
    assert upload(ops_env, content).status_code == 503
    assert work_files(ops_env) == []


def test_confirm_paused_during_update(ops_env):
    upload(ops_env, made_backup(ops_env))
    mark_update_running(ops_env)
    assert confirm(ops_env).status_code == 503
    assert backups(ops_env) == []


# --- while a restore runs, and when the restart fails -------------------------------------------


def test_writes_and_updates_wait_while_a_restore_runs(ops_env):
    ops_env.client.app.state.restore.phase = "running"
    token = csrf_of(ops_env.client, "/settings")
    assert ops_env.client.post("/settings/update", data={"csrf_token": token}).status_code == 503
    assert ops_env.client.post("/settings/backup", data={"csrf_token": token}).status_code == 503
    page = ops_env.client.post("/settings/restart", data={"csrf_token": token})
    assert page.status_code == 503
    assert "A restore is running" in page.text
    assert ops_env.client.app.state.ticker.update_running()


def test_update_start_says_busy_while_a_restore_runs(ops_env, monkeypatch):
    from reprintbuddy.web.routes import ops as routes

    state = ops_env.client.app.state
    monkeypatch.setattr(routes, "is_git_install", lambda _dir: True)
    state.restore.phase = "running"
    monkeypatch.setattr(routes, "restore_blocks", lambda _request: False)  # past the guard
    token = csrf_of(ops_env.client, "/settings")
    response = ops_env.client.post("/settings/update", data={"csrf_token": token})
    assert response.headers["location"].endswith("ops=busy#ops-update")
    assert not any(UPDATE_SERVICE in " ".join(call) for call in ops_env.runner.calls)


def test_failed_restart_keeps_writes_paused_but_allows_sign_in_and_restart(ops_env):
    content = made_backup(ops_env)
    ops_env.runner.on(RESTART_CMD, CommandResult(1, "", "sudo: no"), CommandResult(0))
    upload(ops_env, content)
    response = confirm(ops_env)
    assert response.status_code == 200
    assert "Restart Reprintbuddy now" in response.text
    assert ops_env.client.app.state.restore.phase == "restart_pending"
    assert ops_env.client.app.state.ticker.update_running()

    token = csrf_of(ops_env.client, "/login")
    signed_in = ops_env.client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD})
    assert signed_in.status_code == 303
    token = csrf_of(ops_env.client, "/settings")
    changed = ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"})
    assert changed.status_code == 503
    assert ops_env.client.post("/settings/restart", data={"csrf_token": token}).status_code == 200
    assert ops_env.runner.calls.count(RESTART_CMD) == 2


def test_failed_replace_changes_nothing(ops_env, monkeypatch):
    content = made_backup(ops_env)
    set_queue_cap(ops_env, 9)
    upload(ops_env, content)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(fb, "replace_database", broken)
    response = confirm(ops_env)
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_failed#ops-restore"
    assert ops_env.client.app.state.restore.phase == "idle"
    assert not ops_env.client.app.state.ticker.update_running()
    assert queue_cap(ops_env) == 9
    assert ops_env.runner.calls == []


# --- review findings ---------------------------------------------------------------------------


def staged_token(env):
    page = env.client.get("/settings/restore").text
    import re

    return re.search(r'name="upload_token" value="([^"]+)"', page).group(1)


def confirm_with(env, upload_token, password=""):
    token = csrf_of(env.client, "/settings")
    return env.client.post(
        "/settings/restore",
        data={"csrf_token": token, "backup_password": password, "upload_token": upload_token, "login_password": PASSWORD},
    )


def test_confirm_restores_the_file_the_page_showed(ops_env):
    """Two tabs: the page of the first upload must not restore the second one."""
    first = made_backup(ops_env)
    upload(ops_env, first)
    shown = staged_token(ops_env)
    set_queue_cap(ops_env, 33)
    upload(ops_env, made_backup(ops_env))  # another tab, another file
    set_queue_cap(ops_env, 9)
    response = confirm_with(ops_env, shown)
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_expired#ops-restore"
    assert queue_cap(ops_env) == 9
    assert ops_env.runner.calls == []


def test_confirm_needs_the_upload_token(ops_env):
    upload(ops_env, made_backup(ops_env))
    response = confirm_with(ops_env, "")
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_expired#ops-restore"


def test_confirm_checks_for_an_update_itself(ops_env, monkeypatch):
    from reprintbuddy.web.routes import ops as routes

    upload(ops_env, made_backup(ops_env))
    shown = staged_token(ops_env)
    mark_update_running(ops_env)
    monkeypatch.setattr(routes, "restore_blocks", lambda _request: False)

    async def not_running(_request):
        return False

    monkeypatch.setattr(routes, "update_busy", not_running)  # past the guard
    response = confirm_with(ops_env, shown)
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_busy#ops-restore"
    assert backups(ops_env) == []
    assert ops_env.client.app.state.restore.phase == "idle"  # set before the question, given back after


def test_a_failing_update_question_gives_the_restore_state_back(ops_env, monkeypatch):
    """p96: the state is set before the question is asked; whatever goes wrong, it must not stay set."""
    from reprintbuddy.ops import running

    upload(ops_env, made_backup(ops_env))
    shown = staged_token(ops_env)

    async def breaks(*_args):
        raise RuntimeError("systemd is not answering")

    monkeypatch.setattr(running, "update_running_async", breaks)
    with pytest.raises(RuntimeError):
        confirm_with(ops_env, shown)
    assert ops_env.client.app.state.restore.phase == "idle"  # writes and the engine are not locked out
    assert backups(ops_env) == []
    assert work_files(ops_env) == [] or all(not name.startswith("candidate-") for name in work_files(ops_env))


def test_confirm_checks_for_another_restore_itself(ops_env, monkeypatch):
    from reprintbuddy.web.routes import ops as routes

    upload(ops_env, made_backup(ops_env))
    shown = staged_token(ops_env)
    ops_env.client.app.state.restore.phase = "running"
    monkeypatch.setattr(routes, "restore_blocks", lambda _request: False)
    response = confirm_with(ops_env, shown)
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_busy#ops-restore"
    assert backups(ops_env) == []


def test_upload_checks_for_a_running_restore_itself(ops_env, monkeypatch):
    from reprintbuddy.web.routes import ops as routes

    work = fb.ensure_work_dir(fb.work_dir_for(ops_env.db_path))
    (work / "candidate-of-the-other-tab.db").write_bytes(b"x")
    ops_env.client.app.state.restore.phase = "running"
    monkeypatch.setattr(routes, "restore_blocks", lambda _request: False)
    response = upload(ops_env, made_backup(ops_env))
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_busy#ops-restore"
    assert work_files(ops_env) == ["candidate-of-the-other-tab.db"]


def test_upload_leaves_other_recent_files_alone(ops_env):
    work = fb.ensure_work_dir(fb.work_dir_for(ops_env.db_path))
    (work / "candidate-of-the-other-tab.db").write_bytes(b"x")
    upload(ops_env, made_backup(ops_env))
    assert "candidate-of-the-other-tab.db" in work_files(ops_env)


def test_stale_files_go_when_the_page_is_shown(ops_env):
    import os
    import time

    work = fb.ensure_work_dir(fb.work_dir_for(ops_env.db_path))
    stale = work / "backup-cut-off.zip"
    stale.write_bytes(b"x")
    past = time.time() - 7200
    os.utime(stale, (past, past))
    ops_env.client.get(MAINTENANCE)
    assert work_files(ops_env) == []


def test_upload_form_is_parsed_with_limits(ops_env):
    token = csrf_of(ops_env.client, "/settings")
    response = ops_env.client.post(
        "/settings/restore/upload",
        data={"csrf_token": token, **{f"field{i}": "x" for i in range(10)}},
        files={"backup_file": ("backup.zip", b"x", "application/zip")},
    )
    assert response.status_code == 400
    assert work_files(ops_env) == []


def test_after_the_replace_sign_in_and_restart_are_possible(ops_env, monkeypatch):
    """systemd took the restart, but the process lives on: nobody must be locked out."""
    upload(ops_env, made_backup(ops_env))
    confirm(ops_env)
    assert ops_env.client.app.state.restore.phase == "restart_pending"
    token = csrf_of(ops_env.client, "/login")
    signed_in = ops_env.client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD})
    assert signed_in.status_code == 303
    token = csrf_of(ops_env.client, "/settings")
    assert ops_env.client.post("/settings/restart", data={"csrf_token": token}).status_code == 200


# --- the sign-in password is asked again (decided 07.11.2026, Spec 11.6) -------------------------


class CountingBrake:
    def __init__(self, brake):
        self.brake = brake
        self.failures = 0

    def record_failure(self):
        self.failures += 1
        self.brake.record_failure()

    def current_delay(self):
        return self.brake.current_delay()


def test_pages_ask_for_the_sign_in_password(ops_env):
    page = ops_env.client.get(MAINTENANCE).text
    assert 'name="login_password"' in page and 'autocomplete="current-password"' in page
    upload(ops_env, made_backup(ops_env))
    assert 'name="login_password"' in ops_env.client.get("/settings/restore").text


@pytest.mark.parametrize("login", ["", "wrong-password-1"])
def test_download_needs_the_sign_in_password(ops_env, login):
    brake = CountingBrake(ops_env.client.app.state.login_brake)
    ops_env.client.app.state.login_brake = brake
    response = download(ops_env, BACKUP_PASSWORD, login=login)
    assert response.status_code == 303
    assert response.headers["location"] == f"{MAINTENANCE}&ops=backup_login#ops-backup"
    assert "The sign-in password is wrong. Nothing was downloaded." in ops_env.client.get(response.headers["location"]).text
    assert brake.failures == 1
    assert work_files(ops_env) == []


@pytest.mark.parametrize("login", ["", "wrong-password-1"])
def test_restore_needs_the_sign_in_password(ops_env, login):
    content = made_backup(ops_env)
    set_queue_cap(ops_env, 9)
    upload(ops_env, content)
    brake = CountingBrake(ops_env.client.app.state.login_brake)
    ops_env.client.app.state.login_brake = brake
    response = confirm(ops_env, login=login)
    assert response.status_code == 200
    assert "The sign-in password is wrong. Nothing was changed." in response.text
    assert brake.failures == 1
    assert queue_cap(ops_env) == 9 and backups(ops_env) == [] and ops_env.runner.calls == []
    assert confirm(ops_env).status_code == 200  # the upload is still there
    assert queue_cap(ops_env) == 10


def test_sign_in_password_waits_for_the_brake(ops_env, monkeypatch):
    """Many wrong passwords anywhere slow down this check too: it is the login's way."""
    import asyncio

    waited = []

    async def sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    brake = ops_env.client.app.state.login_brake
    brake.delay = 2.0
    for _ in range(brake.threshold):
        brake.record_failure()
    download(ops_env, login=PASSWORD)
    assert 2.0 in waited


# --- final review -----------------------------------------------------------------------------


def test_restore_says_where_the_backup_listens(ops_env, tmp_path):
    """Address, port and HTTPS come with the backup: the page says where to go after the restart."""
    other = make_client(tmp_path / "other.db")
    with other.app.state.sessionmaker() as session:
        auth.create_admin(session, ADMIN, PASSWORD)
        st.set_setting(session, "port", 8099)
    content = fb.create_backup(tmp_path / "other.db", work_dir=tmp_path / "w", password=None, now=datetime.now(UTC))
    upload(ops_env, content.path.read_bytes())
    response = confirm(ops_env)
    assert response.status_code == 200
    assert "After the restart Reprintbuddy is reachable at http://testserver:8099." in response.text
    assert 'data-health-url="/healthz"' not in response.text  # nothing waits for the old address


def test_unexpected_error_in_the_check_is_a_message(ops_env, monkeypatch):
    upload(ops_env, made_backup(ops_env))

    def broken(*args, **kwargs):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("CREATE TABLE", {}, Exception("database or disk is full"))

    monkeypatch.setattr(fb, "verify_database", broken)
    response = confirm(ops_env)
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_failed#ops-restore"
    assert work_files(ops_env) == []
    assert ops_env.client.app.state.restore.phase == "idle"


def test_restore_waits_for_writes_already_under_way(ops_env):
    """A write that passed the guard just before the restore began must finish first."""
    content = made_backup(ops_env)
    set_queue_cap(ops_env, 9)
    upload(ops_env, content)
    app = ops_env.client.app
    app.state.restore_write_wait = 0.2
    app.state.writes_in_flight += 1  # another request is still writing
    response = confirm(ops_env)
    app.state.writes_in_flight -= 1
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_busy#ops-restore"
    assert app.state.restore.phase == "idle"
    assert queue_cap(ops_env) == 9 and backups(ops_env) == []


def test_writes_are_counted_while_they_run(ops_env, monkeypatch):
    seen = []
    from reprintbuddy.web.routes import settings as settings_routes

    real = settings_routes.st.set_setting

    def spy(*args, **kwargs):
        seen.append(ops_env.client.app.state.writes_in_flight)
        return real(*args, **kwargs)

    monkeypatch.setattr(settings_routes.st, "set_setting", spy)
    post(ops_env, "/settings/language", language="de")
    assert seen and seen[0] == 1
    assert ops_env.client.app.state.writes_in_flight == 0


def test_a_restore_that_begins_while_the_guard_waits_for_the_update_answer_still_pauses_the_write(
    ops_env, monkeypatch
):
    """v0.3.8 p96 review: the question whether an update runs is a thread hop now; a write that
    passed the first look at the restore must not slip into a restore that began during it."""
    from reprintbuddy.web.routes import ops as routes

    app = ops_env.client.app

    async def answer_after_a_restore_began(_request):
        app.state.restore.phase = "running"  # the restore sets its state while the question is out
        return False

    monkeypatch.setattr(routes, "update_busy", answer_after_a_restore_began)
    response = post(ops_env, "/settings/language", language="de")
    assert response.status_code == 503
    assert app.state.writes_in_flight == 0  # it was refused, not counted
    app.state.restore.phase = "idle"
    with app.state.sessionmaker() as session:
        assert st.get_setting(session, "language") != "de"


# --- review of the sign-in check and the write counter ------------------------------------------


def test_changing_the_password_is_braked_like_the_login(ops_env):
    """The sister path: guessing the current password there must cost what a login costs."""
    brake = CountingBrake(ops_env.client.app.state.login_brake)
    ops_env.client.app.state.login_brake = brake
    response = post(
        ops_env,
        "/settings/password",
        current_password="wrong-password-1",
        new_password="a-new-password-1",
        new_password_repeat="a-new-password-1",
    )
    assert "The current password is wrong." in response.text
    assert brake.failures == 1
    with ops_env.client.app.state.sessionmaker() as session:
        assert auth.authenticate(session, ADMIN, PASSWORD) is not None


def test_sign_in_check_runs_through_the_hash_limiter(ops_env):
    limiter = ops_env.client.app.state.hash_limiter
    calls = []
    real = limiter.run

    async def counting(func, *args, **kwargs):
        calls.append(func.__name__)
        return await real(func, *args, **kwargs)

    limiter.run = counting
    download(ops_env)
    assert calls[0] == "authenticate"


def test_restore_waits_for_the_brake_too(ops_env, monkeypatch):
    import asyncio

    upload(ops_env, made_backup(ops_env))
    waited = []

    async def sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    brake = ops_env.client.app.state.login_brake
    brake.delay = 3.0
    for _ in range(brake.threshold):
        brake.record_failure()
    confirm(ops_env)
    assert 3.0 in waited


def test_sign_in_and_setup_are_not_counted_as_writes(ops_env, monkeypatch):
    seen = []
    real = auth.authenticate

    def spy(*args, **kwargs):
        seen.append(ops_env.client.app.state.writes_in_flight)
        return real(*args, **kwargs)

    monkeypatch.setattr(auth, "authenticate", spy)
    token = csrf_of(ops_env.client, "/login")
    ops_env.client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": "wrong-one-1"})
    assert seen == [0]


def test_write_counter_goes_down_after_an_error(ops_env, monkeypatch):
    from reprintbuddy.web.routes import settings as settings_routes

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(settings_routes.st, "set_setting", broken)
    with pytest.raises(RuntimeError):
        post(ops_env, "/settings/language", language="de")
    assert ops_env.client.app.state.writes_in_flight == 0


def test_restore_goes_on_when_the_other_write_ends(ops_env, monkeypatch):
    from reprintbuddy.web.routes import backup as backup_routes

    upload(ops_env, made_backup(ops_env))
    app = ops_env.client.app
    app.state.writes_in_flight += 1
    real_sleep = backup_routes.asyncio.sleep

    async def other_write_ends(seconds):
        if app.state.writes_in_flight > 1:
            app.state.writes_in_flight -= 1
        await real_sleep(0)

    monkeypatch.setattr(backup_routes.asyncio, "sleep", other_write_ends)
    response = confirm(ops_env)
    assert response.status_code == 200
    assert RESTART_CMD in ops_env.runner.calls


def test_an_interrupted_wait_leaves_no_restore_state(ops_env, monkeypatch):
    from reprintbuddy.web.routes import backup as backup_routes

    upload(ops_env, made_backup(ops_env))
    app = ops_env.client.app
    app.state.writes_in_flight += 1

    async def interrupted(seconds):
        raise RuntimeError("cancelled")

    monkeypatch.setattr(backup_routes.asyncio, "sleep", interrupted)
    with pytest.raises(RuntimeError):
        confirm(ops_env)
    app.state.writes_in_flight -= 1
    assert app.state.restore.phase == "idle"
    assert backups(ops_env) == []


def test_restart_is_asked_even_if_the_new_address_cannot_be_read(ops_env, monkeypatch):
    from reprintbuddy.web.routes import backup as backup_routes

    upload(ops_env, made_backup(ops_env))

    def broken(*args, **kwargs):
        raise ValueError("odd port")

    monkeypatch.setattr(backup_routes, "effective_from_settings", broken)
    response = confirm(ops_env)
    assert response.status_code == 200
    assert RESTART_CMD in ops_env.runner.calls


def test_a_restore_is_refused_while_a_click_on_update_has_its_sudo_on_the_way(ops_env, monkeypatch):
    """The write guard already pauses it; this is the restore's own look (the gate may be set after the guard)."""
    from reprintbuddy.web.routes import ops as routes

    async def not_running(_request):
        return False

    monkeypatch.setattr(routes, "update_busy", not_running)  # past the guard
    upload(ops_env, made_backup(ops_env))
    shown = staged_token(ops_env)
    app = ops_env.client.app
    app.state.update_starting = True
    try:
        response = confirm_with(ops_env, shown)
    finally:
        app.state.update_starting = False
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_busy#ops-restore"
    assert app.state.restore.phase == "idle" and backups(ops_env) == []


def test_restore_refusal_before_reading_closes_the_connection(ops_env, monkeypatch):
    """Sister path of the print-file upload (v0.3.9 review): no reading of a refused body."""
    from reprintbuddy.web.routes import backup as backup_routes

    monkeypatch.setattr(backup_routes.fb, "MAX_UPLOAD_BYTES", 10)
    token = csrf_of(ops_env.client, "/settings")
    response = ops_env.client.post(
        "/settings/restore/upload",
        data={"csrf_token": token},
        files={"backup_file": ("b.zip", b"x" * (70 * 1024), "application/zip")},
    )
    assert response.status_code == 303 and response.headers.get("connection") == "close"


def test_restore_closes_the_temporary_file_when_the_token_is_wrong(ops_env, monkeypatch):
    from starlette.datastructures import UploadFile

    closed = []
    original = UploadFile.close

    async def recording(self):
        closed.append(self.filename)
        await original(self)

    monkeypatch.setattr(UploadFile, "close", recording)
    response = ops_env.client.post(
        "/settings/restore/upload",
        data={"csrf_token": "forged"},
        files={"backup_file": ("b.zip", b"PK", "application/zip")},
    )
    assert response.status_code == 403
    assert "b.zip" in closed


def test_restore_waits_for_a_push_message_on_its_way_before_it_copies(ops_env, monkeypatch):
    """p270: the marks are copied only after the message on its way has its outcome written."""
    upload(ops_env, made_backup(ops_env))
    app = ops_env.client.app
    order = []

    async def settle(timeout):
        order.append(("settle", timeout, app.state.restore.active))
        return True

    real = fb.replace_database

    def spy(*args, **kwargs):
        order.append(("replace",))
        return real(*args, **kwargs)

    monkeypatch.setattr(app.state.notifier, "settle", settle)
    monkeypatch.setattr(fb, "replace_database", spy)
    response = confirm(ops_env)
    assert response.status_code == 200
    assert order == [("settle", app.state.restore_notify_wait, True), ("replace",)]


def test_restore_goes_on_when_a_push_message_is_still_on_its_way(ops_env, monkeypatch):
    """The wait is bounded: past it the restore goes ahead (the message may then come once more)."""
    upload(ops_env, made_backup(ops_env))
    app = ops_env.client.app

    async def settle(timeout):
        return False

    monkeypatch.setattr(app.state.notifier, "settle", settle)
    response = confirm(ops_env)
    assert response.status_code == 200
    assert RESTART_CMD in ops_env.runner.calls


def test_restore_is_refused_while_an_update_by_hand_holds_the_lock(ops_env):
    """p91: `reprintbuddy update` typed in the container is no unit; systemd says nothing, the lock does."""
    from reprintbuddy.ops import oplock

    upload(ops_env, made_backup(ops_env))
    app = ops_env.client.app
    held = oplock.acquire(app.state.db_path.parent, "update")
    try:
        response = confirm_with(ops_env, staged_token(ops_env))
    finally:
        held.release()
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_busy#ops-restore"
    assert app.state.restore.phase == "idle"


def test_restore_holds_the_lock_until_the_process_ends(ops_env, monkeypatch):
    """An update typed by hand meanwhile is refused; after a failed replace the lock is free again."""
    from reprintbuddy.ops import oplock

    upload(ops_env, made_backup(ops_env))
    app = ops_env.client.app
    data_dir = app.state.db_path.parent
    seen = []
    real = fb.replace_database

    def spy(*args, **kwargs):
        seen.append(oplock.holder(data_dir))
        return real(*args, **kwargs)

    monkeypatch.setattr(fb, "replace_database", spy)
    assert confirm(ops_env).status_code == 200
    assert seen == ["restore"]
    assert app.state.restore.phase == "restart_pending"
    assert oplock.holder(data_dir) == "restore"  # until the restart ends this process
    app.state.restore.phase = "idle"  # what a test fixture does; the lock goes with it
    assert oplock.holder(data_dir) is None


def test_a_failed_replace_lets_the_lock_go(ops_env, monkeypatch):
    from reprintbuddy.ops import oplock

    upload(ops_env, made_backup(ops_env))
    app = ops_env.client.app

    def fails(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(fb, "replace_database", fails)
    confirm(ops_env)
    assert app.state.restore.phase == "idle"
    assert oplock.holder(app.state.db_path.parent) is None
