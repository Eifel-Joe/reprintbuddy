"""The live channel: GET /live sends change notices to signed-in pages (server-sent events)."""

import asyncio
import html
import re
import time

import pytest
from conftest import make_client, sm_of
from db_probes import Connections, CountingSessions
from factories import make_part
from live_support import open_stream

from reprintbuddy import __version__
from reprintbuddy.domain import stock
from reprintbuddy.web import live as live_channel

COOKIE = "reprintbuddy_session"


@pytest.fixture
async def streams():
    """Open streams of a test; each is closed when the test ends."""
    opened = []

    async def opener(client, path="/live", *, signed_in=True):
        cookie = client.cookies.get(COOKIE) if signed_in else None
        probe = await open_stream(client.app, path, cookie=cookie)
        opened.append(probe)
        return probe

    yield opener
    for probe in opened:
        await probe.aclose()


def new_part(client, name="Insert M4") -> int:
    with sm_of(client).begin() as s:
        return make_part(s, name=name).id


async def wait_until(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def test_signed_out_gets_the_login_page_not_a_stream(ready_client):
    ready_client.cookies.clear()
    response = ready_client.get("/live")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_stream_headers(ready_client, streams):
    probe = await streams(ready_client)
    assert probe.status == 200
    assert probe.headers["content-type"].startswith("text/event-stream")
    assert probe.headers["cache-control"] == "no-store"
    assert probe.headers["x-accel-buffering"] == "no"
    assert "set-cookie" not in probe.headers
    await probe.next_event()
    assert probe.retry == live_channel.RETRY_MS == 2000


async def test_hello_names_state_and_version(ready_client, streams):
    probe = await streams(ready_client)
    app = ready_client.app
    assert await probe.next_event() == ("hello", {"state": app.state.live.state, "version": __version__})


async def test_a_saved_booking_arrives_as_change_within_two_seconds(ready_client, streams):
    app = ready_client.app
    part_id = new_part(ready_client)
    probe = await streams(ready_client)
    assert (await probe.next_event())[0] == "hello"
    with sm_of(ready_client).begin() as s:
        stock.count_part(s, part_id, 5, "user")
    event, data = await probe.next_event(timeout=2.0)
    assert event == "change"
    assert set(data) == {"state", "topics"}
    assert data == {"state": app.state.live.state, "topics": ["journal", "part", f"part:{part_id}"]}


async def test_two_quick_changes_arrive_as_one_event(ready_client, streams, monkeypatch):
    monkeypatch.setattr(live_channel, "COALESCE_SECONDS", 0.2)
    first, second = new_part(ready_client, "Insert M4"), new_part(ready_client, "Insert M5")
    probe = await streams(ready_client)
    assert (await probe.next_event())[0] == "hello"
    with sm_of(ready_client).begin() as s:
        stock.count_part(s, first, 5, "user")
    await asyncio.sleep(0.05)  # the stream has woken up for the first one and now waits
    with sm_of(ready_client).begin() as s:
        stock.count_part(s, second, 3, "user")
    event, data = await probe.next_event(timeout=2.0)
    assert event == "change"
    assert data["topics"] == sorted(["journal", "part", f"part:{first}", f"part:{second}"])
    with pytest.raises(TimeoutError):
        await probe.next_event(timeout=0.5)


async def test_old_page_version_gets_reload_and_the_stream_ends(ready_client, streams):
    probe = await streams(ready_client, "/live?v=0.0.1")
    assert await probe.next_event() == ("reload", {"version": __version__})
    await probe.wait_closed()


async def test_same_page_version_gets_hello(ready_client, streams):
    probe = await streams(ready_client, f"/live?v={__version__}")
    assert (await probe.next_event())[0] == "hello"


@pytest.mark.parametrize("version", ["0.3.0+local.1", "0.3.0-beta.1", "1.0 rc&1"])
async def test_the_address_a_page_carries_gets_hello_for_any_version(ready_client, streams, version):
    """The page sends its version back in /live?v=; a "+" read as a space would mean endless reloads."""
    ready_client.app.state.version = version
    ready_client.app.state.templates.env.globals["app_version"] = version
    page = ready_client.get("/journal").text
    address = html.unescape(re.search(r'data-live="([^"]+)"', page)[1])
    probe = await streams(ready_client, address)
    assert (await probe.next_event())[0] == "hello"


async def test_heartbeat_comment(ready_client, streams, monkeypatch):
    monkeypatch.setattr(live_channel, "HEARTBEAT_SECONDS", 0.05)
    probe = await streams(ready_client)
    assert (await probe.next_event())[0] == "hello"
    assert await probe.next_event(timeout=2.0) == ("comment", "ping")


async def test_stream_ends_after_its_lifetime(ready_client, streams, monkeypatch):
    monkeypatch.setattr(live_channel, "MAX_STREAM_SECONDS", 0.3)
    probe = await streams(ready_client)
    assert (await probe.next_event())[0] == "hello"
    await probe.wait_closed(2)
    assert ready_client.app.state.live.subscriber_count == 0


async def test_disconnect_unsubscribes(ready_client, streams):
    """The default heartbeat (15 s) is far away: the stream ends on `http.disconnect` itself."""
    broker = ready_client.app.state.live
    probe = await streams(ready_client)
    assert (await probe.next_event())[0] == "hello"
    assert broker.subscriber_count == 1
    await probe.disconnect()
    assert await wait_until(lambda: broker.subscriber_count == 0, 1.0)
    await probe.wait_closed(1.0)


async def test_closing_the_broker_ends_open_streams(ready_client, streams):
    probe = await streams(ready_client)
    assert (await probe.next_event())[0] == "hello"
    ready_client.app.state.live.close()
    await probe.wait_closed(2)


async def test_an_open_stream_holds_no_database_connection_or_session(ready_client, streams):
    app = ready_client.app
    connections = Connections(app.state.engine)
    counter = CountingSessions(app.state.sessionmaker)
    app.state.sessionmaker = counter
    probe = await streams(ready_client)
    assert (await probe.next_event())[0] == "hello"
    assert counter.created > 0  # the gate did look at the sign-in
    assert connections.out == 0, "a database connection is held by the open stream"
    assert counter.open == 0, "a database session is left open by the open stream"


async def test_a_new_process_has_a_new_state(ready_client, streams, tmp_path):
    probe = await streams(ready_client)
    _, hello = await probe.next_event()
    other = make_client(tmp_path / "b.db")
    assert hello["state"].split(":")[0] != other.app.state.live.state.split(":")[0]
