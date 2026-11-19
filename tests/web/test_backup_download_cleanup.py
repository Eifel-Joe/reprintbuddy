"""The file of a full backup is deleted when the download ends, however it ends (p103).

The file holds every secret. It used to be deleted by a background task of the response, which
only runs when the response was sent to the end; a browser that cancels the download left it in the
protected folder until the next page that cleans up (up to an hour later) or the next start.
"""

import asyncio
import os
import threading
import time

import httpx
import pytest
import uvicorn
from conftest import PASSWORD, csrf_of

from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.web.routes import backup as backup_routes


def zips_in_work(env):
    work = fb.work_dir_for(env.db_path)
    return sorted(p.name for p in work.iterdir() if p.suffix == ".zip") if work.is_dir() else []


# --- the response class, driven directly ----------------------------------------------------------


def make_file(tmp_path, size=1024):
    path = tmp_path / "backup-x.zip"
    path.write_bytes(os.urandom(size))
    return path


def http_scope(spec="2.3"):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/x",
        "raw_path": b"/x",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


async def drive(response, *, send, receive=None):
    async def default_receive():
        await asyncio.Event().wait()  # never says anything

    await response(http_scope(), receive or default_receive, send)


async def test_the_file_is_removed_after_a_complete_answer(tmp_path):
    path = make_file(tmp_path)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        await asyncio.Event().wait()

    response = backup_routes.RemovedAfterSending(path, media_type="application/zip")
    await asyncio.wait_for(response(http_scope("2.4"), receive, send), 5)
    assert sent and not path.exists()


async def test_the_file_is_removed_when_the_client_is_gone_while_sending(tmp_path):
    path = make_file(tmp_path)

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client gone")  # what a transport may raise for a closed connection

    response = backup_routes.RemovedAfterSending(path, media_type="application/zip")
    with pytest.raises(OSError):
        await asyncio.wait_for(drive(response, send=send), 5)
    assert not path.exists()


async def test_the_file_is_removed_when_the_answer_is_cancelled(tmp_path):
    path = make_file(tmp_path)
    started = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            started.set()
            await asyncio.Event().wait()  # a client that reads nothing

    response = backup_routes.RemovedAfterSending(path, media_type="application/zip")
    task = asyncio.create_task(drive(response, send=send))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not path.exists()


async def test_the_file_is_removed_when_the_client_disconnects(tmp_path):
    """Servers that speak ASGI before 2.4 tell it by `http.disconnect` on `receive`."""
    path = make_file(tmp_path)
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            disconnect.set()
            await asyncio.sleep(0.05)

    response = backup_routes.RemovedAfterSending(path, media_type="application/zip")
    await asyncio.wait_for(drive(response, send=send, receive=receive), 5)
    assert not path.exists()


def test_removing_a_file_that_is_already_gone_is_no_error(tmp_path):
    backup_routes._remove(tmp_path / "gone.zip")


async def test_a_missing_file_is_an_error_and_leaves_nothing(tmp_path):
    response = backup_routes.RemovedAfterSending(tmp_path / "never-there.zip", media_type="application/zip")

    async def send(message):
        pass

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(drive(response, send=send), 5)


# --- through the real server ------------------------------------------------------------------------


def _fatten(env, megabytes):
    """Make the database big enough that its backup is several blocks long."""
    import sqlite3

    con = sqlite3.connect(env.db_path)
    con.execute("CREATE TABLE junk (b BLOB)")
    for _ in range(megabytes):
        con.execute("INSERT INTO junk VALUES (?)", (os.urandom(1024 * 1024),))
    con.commit()
    con.close()


class _HeldAfterTheFirstBlock:
    """Holds the download of the real server after its first block until the test lets it go.

    CI v1.2.0-beta.1 (Python 3.14): the test looked for the file "while the download is under way" and found
    none. Two roots, both in the test, none in the product (a cancelled download is meant to delete its file):
    1. `next(response.iter_bytes(...))` dropped its iterator, and httpx closes the connection when the
       iterator goes (p451 measured it, test_live_shutdown.py). The "cancelled download" began right after the
       first block, and the look at the file raced against the server's clean-up. Measured: the server gets
       `http.disconnect` after the first block while the reader only sleeps.
    2. Nothing held the download. Server and reader share one process and the loopback takes megabytes: with
       the connection kept, a reader that is late by a moment still found the whole file sent and deleted
       (50 MB sent when the first block was read, 2 of 3 runs on Windows).
    With a reader stalled for 3 s the old test failed every time. Now the server cannot end before the test
    has looked, and the connection stays open until it has."""

    def __init__(self, monkeypatch) -> None:
        self.first_block_sent = threading.Event()  # server -> test: the first block is on its way
        self.go_on = threading.Event()  # test -> server: send the rest
        held = self
        original = backup_routes.RemovedAfterSending.__call__

        async def held_call(response, scope, receive, send):
            blocks = 0

            async def held_send(message):
                nonlocal blocks
                await send(message)
                if message["type"] == "http.response.body":
                    blocks += 1
                    if blocks == 1:
                        held.first_block_sent.set()
                        await asyncio.to_thread(held.go_on.wait, 60)  # the event loop stays free

            await original(response, scope, receive, held_send)

        monkeypatch.setattr(backup_routes.RemovedAfterSending, "__call__", held_call)


def test_a_download_the_browser_cancels_leaves_no_file(ops_env, monkeypatch):
    env = ops_env
    _fatten(env, 8)
    held = _HeldAfterTheFirstBlock(monkeypatch)
    config = uvicorn.Config(
        env.client.app, host="127.0.0.1", port=0, timeout_graceful_shutdown=1, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        token = csrf_of(env.client, "/settings")
        cookies = {"reprintbuddy_session": env.client.cookies.get("reprintbuddy_session")}
        data = {"csrf_token": token, "backup_password": "", "backup_password_repeat": "", "login_password": PASSWORD}
        with httpx.Client(timeout=60, cookies=cookies) as http:
            with http.stream("POST", f"http://127.0.0.1:{port}/settings/backup", data=data) as response:
                assert response.status_code == 200
                assert response.headers["content-type"] == "application/zip"
                # Kept: a dropped iterator would close the connection from this side at once (see the class above).
                blocks = response.iter_bytes()
                assert next(blocks)
                assert held.first_block_sent.wait(30), "the server did not send a first block"
                # The server waits now for `go_on`: it cannot have ended, whatever the speed of this side.
                assert zips_in_work(env), "the file should exist while the download is under way"
            # leaving the block closes the connection: a cancelled download
        held.go_on.set()  # the server meets the closed connection with the rest still to send
        deadline = time.monotonic() + 30  # how long to wait for an end that must come, not a bound on its speed
        while zips_in_work(env) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert zips_in_work(env) == [], "the cancelled download left its file behind"
    finally:
        held.go_on.set()
        server.should_exit = True
        thread.join(timeout=10)
