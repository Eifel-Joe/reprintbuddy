"""A cancelled request leaves no decrypted copy (v0.3.11 p140); a full disk keeps the upload (p141).

p140. Root: `await run_in_threadpool(...)` raises `CancelledError` at once when the request task is
cancelled (a closed browser, a shutdown), but the worker thread goes on and finishes the file. The
route's `except Exception` did not even see the cancellation. These tests cancel a real request
(an `httpx` client on the ASGI app, `task.cancel()`) while the worker is held inside the check, then let
the worker go on, and look at the work folder once the worker has ended.

What these tests secure, honestly: through the middleware layers of the app the cancellation reaches the
route only after the worker has ended, so here it is the route's own handling of the cancellation
(`except BaseException`) that removes the file; the flag that stops a worker still running is tested on
its own in tests/ops/test_fullbackup_abandon.py.

p141. A file that cannot be checked because the machine failed (disk full, I/O error) is not a bad
file: it stays, and the page asks to try again without uploading it a second time.
"""

from __future__ import annotations

import asyncio
import errno
import re
import threading
import time

import httpx
import pytest
from conftest import PASSWORD, csrf_of
from starlette.requests import Request

from reprintbuddy.ops import backupcrypto as bc
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.web.routes import backup as backup_routes

from test_ops_full_backup import MAINTENANCE, confirm, download, made_backup, upload, work_files

FAST = bc.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)
BACKUP_PASSWORD = "backup-password-1"
SECRETS = ("candidate-", "rebuilt-", "trial-", "snapshot-", "backup-")


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(fb, "KDF", FAST)


def secret_files(env):
    return [name for name in work_files(env) if name.startswith(SECRETS)]


class Held:
    """Holds a worker thread inside a function until the test lets it go; `done` is set when it returned."""

    def __init__(self, monkeypatch, owner, name):
        self.entered, self.release, self.done = threading.Event(), threading.Event(), threading.Event()
        real = getattr(owner, name)

        def held(*args, **kwargs):
            self.entered.set()
            assert self.release.wait(20)
            try:
                return real(*args, **kwargs)
            finally:
                self.done.set()

        monkeypatch.setattr(owner, name, held)


async def wait_for(event: threading.Event):
    assert await asyncio.to_thread(event.wait, 20)


async def settled(env, *, quiet=0.4):
    """Wait until nothing named like a secret is left and stays away: the worker has ended."""
    deadline = time.monotonic() + 10
    while secret_files(env) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    await asyncio.sleep(quiet)  # a worker that would create the file again gets the time to do it
    return secret_files(env)


def async_client(env) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=env.client.app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=env.client.cookies)


async def token_of(ac: httpx.AsyncClient) -> str:
    page = await ac.get("/settings")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


async def cancel_and_let_go(task: asyncio.Task, held: Held):
    """Cancel the request while the worker is held, then let the worker go on.

    Through the middleware layers of the app the cancellation reaches the route only when the worker
    has ended (the task group waits for the shielded thread); on a bare `run_in_threadpool` it arrives
    at once and the worker goes on. The request is cancelled first and the worker released a moment
    later, so both orders of events are the same test."""
    task.cancel()
    await asyncio.sleep(0.2)
    held.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_for(held.done)


def upload_token(env) -> str:
    page = env.client.get("/settings/restore")
    return re.search(r'name="upload_token" value="([^"]*)"', page.text).group(1)


# --- p140 -------------------------------------------------------------------------------------


async def test_cancelling_the_check_while_it_decrypts_leaves_no_decrypted_copy(ops_env, monkeypatch):
    password = BACKUP_PASSWORD
    upload(ops_env, made_backup(ops_env, password))
    held = Held(monkeypatch, fb, "_open_into")
    async with async_client(ops_env) as ac:
        data = {
            "csrf_token": await token_of(ac),
            "backup_password": password,
            "upload_token": upload_token(ops_env),
            "login_password": PASSWORD,
        }
        task = asyncio.create_task(ac.post("/settings/restore", data=data))
        await wait_for(held.entered)
        await cancel_and_let_go(task, held)
    assert await settled(ops_env) == []


async def test_cancelling_the_check_while_it_rebuilds_leaves_no_decrypted_copy(ops_env, monkeypatch):
    upload(ops_env, made_backup(ops_env, BACKUP_PASSWORD))
    held = Held(monkeypatch, fb, "_copy_rows")
    async with async_client(ops_env) as ac:
        data = {
            "csrf_token": await token_of(ac),
            "backup_password": BACKUP_PASSWORD,
            "upload_token": upload_token(ops_env),
            "login_password": PASSWORD,
        }
        task = asyncio.create_task(ac.post("/settings/restore", data=data))
        await wait_for(held.entered)
        await cancel_and_let_go(task, held)
    assert await settled(ops_env) == []
    assert ops_env.client.app.state.restore.phase == "idle"
    assert ops_env.runner.calls == []  # nothing was replaced, no restart asked for


async def test_cancelling_a_download_leaves_no_backup_file(ops_env, monkeypatch):
    """Sister path: the finished backup file of a cancelled download holds every secret too."""
    held = Held(monkeypatch, fb, "create_backup")
    async with async_client(ops_env) as ac:
        data = {
            "csrf_token": await token_of(ac),
            "backup_password": "",
            "backup_password_repeat": "",
            "login_password": PASSWORD,
        }
        task = asyncio.create_task(ac.post("/settings/backup", data=data))
        await wait_for(held.entered)
        await cancel_and_let_go(task, held)
    assert await settled(ops_env) == []


async def test_an_untouched_request_still_works_through_the_async_client(ops_env):
    """The cancel tests stand on this: the same request, not cancelled, restores."""
    upload(ops_env, made_backup(ops_env, BACKUP_PASSWORD))
    async with async_client(ops_env) as ac:
        data = {
            "csrf_token": await token_of(ac),
            "backup_password": BACKUP_PASSWORD,
            "upload_token": upload_token(ops_env),
            "login_password": PASSWORD,
        }
        response = await ac.post("/settings/restore", data=data)
    assert response.status_code == 200
    assert secret_files(ops_env) == []


# --- p141 -------------------------------------------------------------------------------------


def disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


def test_a_full_disk_in_the_check_keeps_the_upload_and_asks_to_try_again(ops_env, monkeypatch):
    upload(ops_env, made_backup(ops_env))
    token = upload_token(ops_env)
    monkeypatch.setattr(fb, "verify_database", disk_full)
    response = confirm(ops_env)
    assert response.status_code == 200
    assert "file is not the cause" in response.text and "try again" in response.text.lower()
    assert f'value="{token}"' in response.text  # the same upload, on the same page
    assert 'action="/settings/restore"' in response.text
    assert [n for n in work_files(ops_env) if n.startswith("upload-")] != []
    assert [n for n in secret_files(ops_env) if n.startswith("candidate-")] == []
    assert ops_env.client.app.state.restore.phase == "idle"
    assert ops_env.client.get("/settings/restore").status_code == 200  # still staged for this browser

    monkeypatch.undo()  # the disk has room again: the second press needs no new upload
    response = confirm(ops_env)
    assert response.status_code == 200 and "restart" in response.text.lower()
    assert secret_files(ops_env) == []


def test_a_full_disk_in_the_check_of_an_encrypted_backup_keeps_the_password_field(ops_env, monkeypatch):
    upload(ops_env, made_backup(ops_env, BACKUP_PASSWORD))
    monkeypatch.setattr(fb, "verify_database", disk_full)
    response = confirm(ops_env, BACKUP_PASSWORD)
    assert response.status_code == 200 and "file is not the cause" in response.text
    assert 'name="backup_password"' in response.text


def test_a_full_disk_while_replacing_keeps_the_upload_too(ops_env, monkeypatch):
    upload(ops_env, made_backup(ops_env))
    monkeypatch.setattr(fb, "replace_database", disk_full)
    response = confirm(ops_env)
    assert response.status_code == 200 and "file is not the cause" in response.text
    assert [n for n in work_files(ops_env) if n.startswith("upload-")] != []
    assert secret_files(ops_env) == []
    assert ops_env.client.app.state.restore.phase == "idle"
    assert not ops_env.client.app.state.ticker.update_running()
    assert ops_env.runner.calls == []


def test_an_error_of_sqlite_with_a_machine_code_is_one_too(ops_env, monkeypatch):
    import sqlite3

    upload(ops_env, made_backup(ops_env))

    def io_error(*args, **kwargs):
        error = sqlite3.OperationalError("disk I/O error")
        error.sqlite_errorcode = 10  # SQLITE_IOERR
        raise error

    monkeypatch.setattr(fb, "verify_database", io_error)
    assert "file is not the cause" in confirm(ops_env).text


def test_an_error_nobody_expected_still_discards_the_upload(ops_env, monkeypatch):
    upload(ops_env, made_backup(ops_env))

    def odd(*args, **kwargs):
        raise RuntimeError("something else")

    monkeypatch.setattr(fb, "verify_database", odd)
    response = confirm(ops_env)
    assert response.headers["location"] == f"{MAINTENANCE}&ops=restore_failed#ops-restore"
    assert work_files(ops_env) == []


def test_a_damaged_file_and_a_wrong_password_are_as_before(ops_env):
    upload(ops_env, made_backup(ops_env, BACKUP_PASSWORD))
    response = confirm(ops_env, "not-the-password-1")
    assert response.status_code == 200 and "file is not the cause" not in response.text
    assert [n for n in work_files(ops_env) if n.startswith("upload-")] != []  # a wrong password keeps the upload


def test_the_download_text_of_a_full_disk_is_unchanged(ops_env, monkeypatch):
    monkeypatch.setattr(fb, "create_backup", disk_full)
    response = download(ops_env)
    assert response.headers["location"] == f"{MAINTENANCE}&ops=backup_failed#ops-backup"


async def test_cancelling_while_waiting_for_a_running_tick_gives_the_restore_state_back(ops_env):
    """The restore waits for the tick lock after the check: a shutdown that cancels it there leaves neither
    the decrypted copy nor a restore state stuck on "running" (found in the review of v0.3.11)."""
    upload(ops_env, made_backup(ops_env, BACKUP_PASSWORD))
    app = ops_env.client.app
    ticker = app.state.ticker
    tick_ends = asyncio.Event()

    async def a_running_tick():
        async with ticker.hold():  # the same lock a tick takes
            await tick_ends.wait()

    running_tick = asyncio.create_task(a_running_tick())
    await asyncio.sleep(0)
    assert ticker.held()
    try:
        async with async_client(ops_env) as ac:
            data = {
                "csrf_token": await token_of(ac),
                "backup_password": BACKUP_PASSWORD,
                "upload_token": upload_token(ops_env),
                "login_password": PASSWORD,
            }
            task = asyncio.create_task(ac.post("/settings/restore", data=data))
            deadline = time.monotonic() + 20
            while not (app.state.restore.phase == "running" and ticker.waiting()):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            assert [n for n in secret_files(ops_env) if n.startswith("candidate-")] != []  # the copy is there
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert ticker.waiting() == 0
    finally:
        tick_ends.set()
        await running_tick
    assert app.state.restore.phase == "idle"
    assert await settled(ops_env) == []
    assert ops_env.runner.calls == []


async def test_cancelling_once_the_replace_worker_has_begun_leaves_the_restore_pending(ops_env, monkeypatch):
    """From the moment the worker began the database may be replaced: the state must not fall back to
    "idle" (writes would go on against the state of the old database); the restart button stays usable."""
    upload(ops_env, made_backup(ops_env, BACKUP_PASSWORD))
    held = Held(monkeypatch, fb, "replace_database")
    app = ops_env.client.app
    async with async_client(ops_env) as ac:
        data = {
            "csrf_token": await token_of(ac),
            "backup_password": BACKUP_PASSWORD,
            "upload_token": upload_token(ops_env),
            "login_password": PASSWORD,
        }
        task = asyncio.create_task(ac.post("/settings/restore", data=data))
        await wait_for(held.entered)
        await cancel_and_let_go(task, held)
    assert app.state.restore.phase == "restart_pending"
    assert await settled(ops_env) == []  # the worker ended the replace and removed the copy itself
    assert ops_env.runner.calls == []


def test_the_replace_gate_lets_exactly_one_side_win():
    from reprintbuddy.web.routes.backup import _ReplaceGate

    worker_first = _ReplaceGate()
    assert worker_first.begin() is True and worker_first.abort() is False
    route_first = _ReplaceGate()
    assert route_first.abort() is True and route_first.begin() is False


async def test_a_failing_replace_worker_leaves_no_copy_and_asks_to_try_again(ops_env, monkeypatch):
    upload(ops_env, made_backup(ops_env, BACKUP_PASSWORD))

    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(fb, "replace_database", fail)
    async with async_client(ops_env) as ac:
        data = {
            "csrf_token": await token_of(ac),
            "backup_password": BACKUP_PASSWORD,
            "upload_token": upload_token(ops_env),
            "login_password": PASSWORD,
        }
        response = await ac.post("/settings/restore", data=data)
    assert response.status_code == 200 and "file is not the cause" in response.text
    assert await settled(ops_env) == []


# --- p149 (v0.3.12): a cancellation that reaches the route at once, without the app's middleware -------------
#
# Through the app's middleware the cancellation of a download arrives only after the worker has ended (see the
# module docstring); a server or a middleware stack without that layer delivers it at once. These tests call the
# route function itself, so `run_in_threadpool` raises `CancelledError` while the worker still runs.


def bare_request(env, path: str, body: bytes, content_type: str) -> Request:
    """A request for the route function alone: no middleware, no session middleware, no CSRF check."""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Event().wait()  # the body is read; the client says nothing more
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())],
        "query_string": b"",
        "app": env.client.app,
        "session": {},
    }
    return Request(scope, receive)


async def cancel_bare(coro, held: Held):
    """Cancel the route while the worker is held: the cancellation arrives at once; then the worker goes on."""
    task = asyncio.create_task(coro)
    await wait_for(held.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    held.release.set()
    await wait_for(held.done)


@pytest.mark.parametrize("where", ["create_backup", "_write_member"])
@pytest.mark.parametrize("password", ["", BACKUP_PASSWORD])
async def test_a_download_cancelled_without_middleware_leaves_no_backup_file(ops_env, monkeypatch, where, password):
    async def confirmed(request, db, login):
        return True

    monkeypatch.setattr(backup_routes, "password_confirmed", confirmed)
    held = Held(monkeypatch, fb, where)
    body = f"backup_password={password}&backup_password_repeat={password}&login_password=x".encode()
    request = bare_request(ops_env, "/settings/backup", body, "application/x-www-form-urlencoded")
    await cancel_bare(backup_routes.download_backup(request, db=None), held)
    assert await settled(ops_env) == []


@pytest.mark.parametrize("where", ["save_upload", "read_manifest"])
async def test_an_upload_cancelled_without_middleware_leaves_no_uploaded_file(ops_env, monkeypatch, where):
    """While saving, today's code is saved by accident (closing the upload breaks the worker's read); while
    reading the manifest the file is complete and nothing removed it."""

    async def no_check(request):
        return None

    monkeypatch.setattr(backup_routes, "verify_csrf", no_check)
    content = made_backup(ops_env)  # unencrypted: it holds every secret
    held = Held(monkeypatch, fb, where)
    boundary = "p149boundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"backup_file\"; filename=\"b.zip\"\r\n"
        "Content-Type: application/zip\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    request = bare_request(ops_env, "/settings/restore/upload", body, f"multipart/form-data; boundary={boundary}")
    await cancel_bare(backup_routes.restore_upload(request), held)
    assert await settled(ops_env) == []
    assert [n for n in work_files(ops_env) if n.startswith("upload-")] == []
