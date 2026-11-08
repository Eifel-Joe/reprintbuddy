"""The tick engine inside the web app (plan B4 Task 11, decisions 12, 13, 16)."""

import time
from datetime import UTC, datetime, timedelta

import sqlalchemy

from conftest import ADMIN, PASSWORD, csrf_of, form_token_of, make_client, run_tick, sm_of
from factories import make_article
from reprintbuddy.domain.engine import Engine
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.service import SYSTEMCTL, UPDATE_SERVICE
from reprintbuddy.ops.status import UpdateStatus, write_status

SHOW_STATE = (SYSTEMCTL, "show", "--property=ActiveState", "--value", UPDATE_SERVICE)


def count_tick_requests(client) -> list[int]:
    """Replace `request_tick` of the app's ticker with a counter."""
    calls: list[int] = []
    client.app.state.ticker.request_tick = lambda: calls.append(1)
    return calls


def sale(client, article_id, qty):
    data = {
        "csrf_token": csrf_of(client, "/bookings"),
        "form_token": form_token_of(client, "/bookings", "/bookings/sale"),
        "article_id": article_id,
        "qty": qty,
    }
    return client.post("/bookings/sale", data=data)


def test_app_has_ticker(ready_client):
    assert isinstance(ready_client.app.state.ticker, Engine)
    # The SQLAlchemy engine keeps its name (B3 uses it).
    assert isinstance(ready_client.app.state.engine, sqlalchemy.engine.Engine)


def test_successful_write_requests_tick(ready_client):
    with sm_of(ready_client).begin() as s:
        article = make_article(s, name="Template A", stock=3).id
    calls = count_tick_requests(ready_client)

    assert sale(ready_client, article, "1").status_code == 303
    assert len(calls) == 1

    # A refused booking answers 200 with a message: a tick then is harmless.
    assert sale(ready_client, article, "99").status_code == 200
    assert len(calls) == 2

    # Refused before the route (no CSRF): no tick.
    assert ready_client.post("/bookings/sale", data={"article_id": article, "qty": "1"}).status_code == 403
    assert len(calls) == 2

    # Reading is not writing.
    assert ready_client.get("/articles").status_code == 200
    assert len(calls) == 2

    # Signing in writes a session, but it is not a booking.
    ready_client.cookies.clear()
    token = csrf_of(ready_client, "/login")
    login = ready_client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD})
    assert login.status_code == 303
    assert len(calls) == 2


def test_lifespan_runs_and_stops_loop(db_path):
    # A fresh database without setup: no Bambuddy client, so no network call.
    client = make_client(db_path, run_engine=True)
    ticker = client.app.state.ticker
    with client:
        deadline = time.monotonic() + 5
        while ticker.status.last_tick_at is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ticker.status.last_tick_at is not None
        task = client.app.state.ticker_task
        assert not task.done()
    assert task.done()


def test_engine_not_started_without_run_engine(db_path):
    client = make_client(db_path)
    with client:
        assert client.app.state.ticker_task is None
        assert client.app.state.ticker.status.last_tick_at is None


def test_update_running_pauses_ticker(ops_env):
    write_status(
        ops_env.db_path.parent,
        UpdateStatus(state="running", step="install", started_at=datetime.now(UTC) - timedelta(minutes=5)),
    )
    ops_env.runner.on(SHOW_STATE, CommandResult(0, "active\n"))
    result = run_tick(ops_env.client.app.state.ticker)
    assert result.skipped == "update"


def test_the_engine_is_told_cheaply_when_a_restore_or_an_update_has_just_begun(ready_client):
    """v0.3.8 p96 review: the synchronous look right before a print is marked as sent."""
    from datetime import UTC, datetime

    from reprintbuddy.ops.status import UpdateStatus, write_status

    app = ready_client.app
    assert app.state.ticker.update_started() is False
    app.state.restore.phase = "running"
    assert app.state.ticker.update_started() is True
    app.state.restore.phase = "idle"
    write_status(app.state.db_path.parent, UpdateStatus(state="queued", step="start", started_at=datetime.now(UTC)))
    assert app.state.ticker.update_started() is True
