"""Requests that overlap: no lost changes, no locked database, no frozen pool."""

import asyncio
import re
import time

import httpx
import pytest
from conftest import ADMIN, PASSWORD, auth_me

from reprintbuddy import settings as st

COOKIE = "reprintbuddy_session"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')


def asgi_client(app, cookie: str | None = None) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", follow_redirects=False
    )
    if cookie:
        client.cookies.set(COOKIE, cookie)
    return client


def token_of(response: httpx.Response) -> str:
    return TOKEN.search(response.text).group(1)


async def slow_check(_request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(0.6)
    return httpx.Response(200, json=auth_me(username="other-user"))


async def test_a_slow_bambuddy_save_and_a_parallel_language_save_both_land(ready_client):
    """Read, wait on the network, write: another request may commit in between."""
    app = ready_client.app
    app.state.http_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(slow_check))
    async with asgi_client(app, ready_client.cookies.get(COOKIE)) as client:
        token = token_of(await client.get("/settings"))
        slow = asyncio.create_task(
            client.post(
                "/settings/bambuddy",
                data={
                    "csrf_token": token,
                    # the same address written differently: the first write is a real change
                    "bambuddy_address": "http://BAMBUDDY.test:8000",
                    "bambuddy_token": "",
                },
            )
        )
        await asyncio.sleep(0.2)  # the slow request has read its settings and now waits
        fast = await client.post("/settings/language", data={"csrf_token": token, "language": "de"})
        slow_response = await slow
    assert fast.status_code == 303
    assert slow_response.status_code == 303
    with app.state.sessionmaker() as s:
        assert st.get_setting(s, "language") == "de"
        assert st.get_setting(s, "bambuddy_url") == "http://BAMBUDDY.test:8000"
        assert st.get_setting(s, "bambuddy_username") == "other-user"
        assert st.get_setting(s, "bambuddy_api_key") == "bb_testkey"


# --- one database session per request, no connection held while waiting -----------


class CountingSessions:
    """Stands in for the sessionmaker and counts how many sessions are open at once."""

    def __init__(self, inner):
        self.inner = inner
        self.open = 0
        self.most = 0
        self.created = 0

    def __call__(self):
        session = self.inner()
        self.open += 1
        self.created += 1
        self.most = max(self.most, self.open)
        real_close = session.close
        closed = []

        def close():
            if not closed:
                closed.append(True)
                self.open -= 1
            real_close()

        session.close = close
        return session


class Connections:
    """Counts database connections that are checked out right now."""

    def __init__(self, engine):
        from sqlalchemy import event

        self.out = 0
        event.listen(engine, "checkout", lambda *a: setattr(self, "out", self.out + 1))
        event.listen(engine, "checkin", lambda *a: setattr(self, "out", self.out - 1))


def test_a_request_has_at_most_one_session_open(ready_client):
    app = ready_client.app
    counter = CountingSessions(app.state.sessionmaker)
    app.state.sessionmaker = counter
    token = None

    def check(label):
        assert counter.most <= 1, f"{label}: {counter.most} sessions open at once"
        assert counter.open == 0, f"{label}: a session was left open"

    for path in ("/", "/settings", "/no-such-page", "/login", "/healthz"):
        ready_client.get(path)
        check(f"GET {path}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', ready_client.get("/settings").text).group(1)
    ready_client.post("/settings/language", data={"csrf_token": token, "language": "en"})
    check("POST /settings/language")
    ready_client.post("/logout", data={})  # refused: CSRF
    check("refused POST")
    ready_client.cookies.clear()
    ready_client.get("/settings")  # redirect to the login page
    check("redirect")
    login_token = re.search(r'name="csrf_token" value="([^"]+)"', ready_client.get("/login").text).group(1)
    ready_client.post("/login", data={"csrf_token": login_token, "username": ADMIN, "password": "wrong-password"})
    check("failed login")
    assert counter.created > 0


def test_setup_requests_have_at_most_one_session_open(client):
    counter = CountingSessions(client.app.state.sessionmaker)
    client.app.state.sessionmaker = counter
    client.get("/setup")
    client.get("/")  # redirect to /setup
    assert counter.most <= 1 and counter.open == 0


async def test_twenty_concurrent_page_requests_all_finish_quickly(ready_client):
    app = ready_client.app
    async with asgi_client(app) as client:  # signed out: every request ends on the login page
        started = time.monotonic()
        responses = await asyncio.wait_for(
            asyncio.gather(*[client.get("/login") for _ in range(20)]), timeout=15
        )
    assert [r.status_code for r in responses] == [200] * 20
    assert time.monotonic() - started < 10


async def test_twelve_concurrent_wrong_logins_all_finish(ready_client):
    app = ready_client.app
    app.state.login_failure_delay = 0.2
    async with asgi_client(app) as client:
        token = token_of(await client.get("/login"))
        started = time.monotonic()
        responses = await asyncio.wait_for(
            asyncio.gather(
                *[
                    client.post(
                        "/login", data={"csrf_token": token, "username": ADMIN, "password": "wrong-password"}
                    )
                    for _ in range(12)
                ]
            ),
            timeout=20,
        )
    assert [r.status_code for r in responses] == [200] * 12
    assert time.monotonic() - started < 15


async def test_no_connection_is_held_while_a_failed_login_waits(ready_client):
    app = ready_client.app
    connections = Connections(app.state.engine)
    app.state.login_failure_delay = 0.6
    async with asgi_client(app) as client:
        token = token_of(await client.get("/login"))
        waiting = asyncio.create_task(
            client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": "wrong-password"})
        )
        await asyncio.sleep(0.35)  # past the hashing, inside the pause
        assert not waiting.done()
        assert connections.out == 0, "a database connection is held during the pause"
        await waiting


async def test_no_connection_is_held_while_bambuddy_answers(ready_client):
    app = ready_client.app
    connections = Connections(app.state.engine)
    app.state.http_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(slow_check))
    async with asgi_client(app, ready_client.cookies.get(COOKIE)) as client:
        token = token_of(await client.get("/settings"))
        saving = asyncio.create_task(
            client.post(
                "/settings/bambuddy",
                data={
                    "csrf_token": token,
                    "bambuddy_address": "http://bambuddy.test:8000",
                    "bambuddy_token": "",
                },
            )
        )
        await asyncio.sleep(0.3)  # the check is running
        assert not saving.done()
        assert connections.out == 0, "a database connection is held while waiting for Bambuddy"
        await saving
