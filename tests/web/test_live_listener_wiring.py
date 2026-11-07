"""Bambuddy's live channel feeds the tick engine (Spec 7.6, plan B4 Task 11).

A message that may have changed the queue, and every (re)connect, only flag the engine for a
poll (`Engine.request_poll`); the poll itself runs in the engine's own loop. The connection is
a script passed as `live_connect`; no socket is opened and no test waits for real time.
"""

import asyncio

from conftest import make_client
from reprintbuddy.bambuddy.client import BambuddyUnavailable
from reprintbuddy.bambuddy.fake import FakeBambuddy


class Connection:
    """`recv` plays the script, then stays open and quiet."""

    def __init__(self, script):
        self.script = list(script)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def recv(self):
        if self.script:
            return self.script.pop(0)
        await asyncio.Event().wait()


class Connect:
    def __init__(self, *script):
        self.script = script
        self.urls: list[str] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        return Connection(self.script)


class Channel:
    """What the app's `bambuddy_factory` returns when Bambuddy is set up."""

    async def ws_token(self) -> str:
        return "tok"

    def ws_url(self, token: str) -> str:
        return f"ws://bambuddy.test/api/v1/ws?token={token}"


async def never_sleep(_seconds: float) -> None:
    await asyncio.Event().wait()


def polls_of(app) -> asyncio.Queue:
    """Replace `request_poll` of the app's ticker; every call lands in the queue."""
    queue: asyncio.Queue = asyncio.Queue()
    app.state.ticker.request_poll = lambda: queue.put_nowait("poll")
    return queue


async def wait_for(condition, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def make_app(db_path, connect, *, factory=lambda: Channel(), run_engine=True):
    app = make_client(
        db_path, run_engine=run_engine, live_connect=connect, live_sleep=never_sleep
    ).app
    app.state.bambuddy_factory = factory
    return app


async def test_connect_requests_a_poll(db_path):
    connect = Connect()
    app = make_app(db_path, connect)
    polls = polls_of(app)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(polls.get(), 5)
        assert app.state.live_listener.state == "connected"
    assert connect.urls == ["ws://bambuddy.test/api/v1/ws?token=tok"]


async def test_print_complete_requests_a_poll(db_path):
    connect = Connect('{"type": "print_complete"}', '{"type": "printer_status"}')
    app = make_app(db_path, connect)
    polls = polls_of(app)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(polls.get(), 5)  # "connected"
        await asyncio.wait_for(polls.get(), 5)  # print_complete
        await asyncio.sleep(0.05)
        assert polls.empty()  # printer_status is no trigger


async def test_not_configured_waits_without_connecting(db_path):
    connect = Connect()
    app = make_app(db_path, connect, factory=lambda: None)
    polls = polls_of(app)
    async with app.router.lifespan_context(app):
        await wait_for(lambda: app.state.live_listener.state == "waiting")
    assert connect.urls == []
    assert polls.empty()


async def test_client_without_a_channel_counts_as_unavailable(db_path):
    connect = Connect()
    app = make_app(db_path, connect, factory=FakeBambuddy)  # no ws_token
    async with app.router.lifespan_context(app):
        await wait_for(lambda: app.state.live_listener.state == "waiting")
    assert connect.urls == []


async def test_changed_settings_take_effect_at_the_next_connect(db_path):
    connect = Connect()
    channels = [None, Channel()]
    app = make_app(db_path, connect, factory=lambda: channels[0])
    adapter = app.state.live_listener._client
    with_none = None
    try:
        await adapter.ws_token()
    except BambuddyUnavailable as exc:
        with_none = exc
    assert with_none is not None
    channels[0] = channels[1]  # Bambuddy is set up now; the adapter asks the factory afresh
    assert await adapter.ws_token() == "tok"
    assert adapter.ws_url("t") == "ws://bambuddy.test/api/v1/ws?token=t"


async def test_shutdown_cancels_the_listener(db_path):
    app = make_app(db_path, Connect())
    polls = polls_of(app)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(polls.get(), 5)
        task = app.state.live_task
        assert task.get_name() == "bambuddy-live"
        assert not task.done()
    assert task.done()
    assert not [t for t in asyncio.all_tasks() if t.get_name() in ("bambuddy-live", "ticker")]


async def test_no_listener_without_run_engine(db_path):
    connect = Connect()
    app = make_app(db_path, connect, run_engine=False)
    async with app.router.lifespan_context(app):
        assert app.state.live_task is None
        await asyncio.sleep(0.05)
    assert connect.urls == []
