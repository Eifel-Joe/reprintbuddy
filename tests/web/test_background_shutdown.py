"""At shutdown every background task is stopped and waited for together (v0.3.8 p67)."""

import asyncio
import logging

import httpx
from conftest import auth_me, http_client_factory, make_client

from reprintbuddy.web import app as app_module
from reprintbuddy.web.app import stop_background
from reprintbuddy.web.rights import RightsMonitor


async def quiet_task(name="quiet"):
    async def run():
        await asyncio.Event().wait()

    return asyncio.create_task(run(), name=name)


async def test_all_tasks_are_cancelled_and_waited_for():
    tasks = [await quiet_task("a"), await quiet_task("b"), await quiet_task("c")]
    await asyncio.sleep(0)
    await stop_background(tasks)
    assert all(task.cancelled() for task in tasks)


async def test_a_task_that_fails_while_stopping_does_not_hide_the_others(caplog):
    """The old loop awaited one task after the other: an error in the first left the rest unwaited."""

    async def fails_on_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("http://bambuddy.test:8000 SECRET-KEY") from None

    failing = asyncio.create_task(fails_on_cancel(), name="failing")
    later = [await quiet_task("later-1"), await quiet_task("later-2")]
    await asyncio.sleep(0)
    with caplog.at_level(logging.WARNING, logger="reprintbuddy.app"):
        await stop_background([failing, *later])
    assert all(task.done() for task in later) and all(task.cancelled() for task in later)
    assert "background task failing ended with RuntimeError" in caplog.text
    assert "SECRET-KEY" not in caplog.text and "bambuddy.test" not in caplog.text  # the type only


async def test_a_task_that_already_died_is_looked_at_too(caplog):
    async def dies():
        raise ValueError("early")

    task = asyncio.create_task(dies(), name="died-early")
    await asyncio.sleep(0.01)
    with caplog.at_level(logging.WARNING, logger="reprintbuddy.app"):
        await stop_background([task])
    assert "background task died-early ended with ValueError" in caplog.text


async def test_a_task_that_ignores_the_cancel_is_named_and_does_not_hold_the_shutdown(caplog):
    release = asyncio.Event()

    async def stubborn():
        while not release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass  # refuses to stop

    task = asyncio.create_task(stubborn(), name="stubborn")
    other = await quiet_task("other")
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    began = loop.time()
    with caplog.at_level(logging.WARNING, logger="reprintbuddy.app"):
        await stop_background([task, other], wait=0.2)
    assert loop.time() - began < 2
    assert other.cancelled()
    assert "background task stubborn did not stop within 0.2 s" in caplog.text
    release.set()
    await task


async def test_no_tasks_is_fine():
    await stop_background([])


def test_the_default_time_is_a_few_seconds():
    assert 1 <= app_module.SHUTDOWN_TASK_WAIT <= 10


# --- the app's own lifespan ----------------------------------------------------------------------------------


async def test_the_lifespan_stops_a_rights_check_a_page_started(db_path, respx_mock, monkeypatch):
    """A page starts the check of the key's rights in the background; nobody waited for it at shutdown."""
    monkeypatch.setattr("reprintbuddy.web.rights.FIRST_WAIT_SECONDS", 0.05)  # the page does not wait for it
    release = asyncio.Event()

    async def hangs(request):
        await release.wait()
        return httpx.Response(200, json=auth_me())

    respx_mock.get("http://bambuddy.test:8000/api/v1/auth/me").mock(side_effect=hangs)
    client = make_client(db_path)
    app = client.app
    monitor: RightsMonitor = app.state.rights
    async with app.router.lifespan_context(app):
        monitor.status = "unknown"
        await monitor.ensure(lambda: ("http://bambuddy.test:8000", "bb_testkey"), http_client_factory)
        task = monitor.task
        assert task is not None and not task.done()
    assert task.done() and task.cancelled()
    assert monitor.task is None
