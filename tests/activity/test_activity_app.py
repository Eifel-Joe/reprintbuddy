"""The activity log in the running app (Spec 10.7): "started" at every start, a finished restore in the
restored database, and the writer counted as a write in flight like a request."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from conftest import make_client, sm_of
from reprintbuddy import __version__
from reprintbuddy.activity.writer import write_restored_marker
from reprintbuddy.models import ActivityEntry


def lines(client) -> list[ActivityEntry]:
    with sm_of(client)() as s:
        return list(s.scalars(select(ActivityEntry).order_by(ActivityEntry.id)))


@pytest.mark.fresh_database  # also the very first start, on a database the app creates (R1)
def test_every_start_is_logged_with_the_version(db_path):
    client = make_client(db_path)
    with client:
        client.app.state.activity.drain()
    [line] = lines(client)
    assert (line.kind, line.actor, line.details) == ("started", "system", {"version": __version__})


def test_a_restore_is_logged_by_the_next_start(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    write_restored_marker(db_path.parent, created_at=datetime(2026, 11, 1, 8, 0, tzinfo=UTC), version="0.3.12", via=None)
    client = make_client(db_path)
    with client:
        client.app.state.activity.drain()
    restored, started = lines(client)
    assert (restored.kind, restored.actor, restored.via) == ("restore_done", "user", None)
    assert restored.details == {"created_at": "2026-11-01T08:00:00+00:00", "version": "0.3.12"}
    assert started.kind == "started"
    assert not (db_path.parent / "activity-restored.json").exists()


def test_a_restore_from_the_command_line_says_so(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    write_restored_marker(db_path.parent, created_at=datetime(2026, 11, 1, 8, 0, tzinfo=UTC), version="0.3.12", via="cli")
    client = make_client(db_path)
    with client:
        client.app.state.activity.drain()
    assert lines(client)[0].via == "cli"


def test_nothing_is_written_while_a_restore_runs(db_path):
    client = make_client(db_path)
    app = client.app
    app.state.activity.record("login", "user")
    app.state.restore.phase = "running"
    assert asyncio.run(app.state.activity.drain_counted()) is False
    app.state.restore.phase = "idle"
    assert asyncio.run(app.state.activity.drain_counted()) is True
    assert [line.kind for line in lines(client)] == ["login"]


def test_a_run_is_a_write_in_flight_of_the_app(db_path):
    """The restore waits while `app.state.writes_in_flight` is above its own request."""
    client = make_client(db_path)
    app = client.app
    activity = app.state.activity
    entered, release = threading.Event(), threading.Event()
    real = activity.drain

    def slow() -> int:
        entered.set()
        release.wait(5)
        return real()

    activity.drain = slow
    activity.record("login", "user")

    async def scenario() -> tuple[int, int]:
        task = asyncio.create_task(activity.drain_counted())
        while not entered.is_set():
            await asyncio.sleep(0.01)
        during = app.state.writes_in_flight
        release.set()
        await task
        return during, app.state.writes_in_flight

    assert asyncio.run(scenario()) == (1, 0)
