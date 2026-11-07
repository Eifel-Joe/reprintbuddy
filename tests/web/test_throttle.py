"""Password hashing is limited to a few runs at once; many failed logins slow everybody down."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import ADMIN, PASSWORD, csrf_of, make_client
from test_concurrency import asgi_client, token_of

from reprintbuddy import auth
from reprintbuddy.web.app import create_app
from reprintbuddy.web.throttle import FailureBrake, HashLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- the brake itself -----------------------------------------------------------------------


def test_brake_is_off_until_the_threshold():
    clock = Clock()
    brake = FailureBrake(threshold=10, window=60, delay=2.0, clock=clock)
    for _ in range(9):
        brake.record_failure()
    assert brake.current_delay() == 0
    brake.record_failure()
    assert brake.current_delay() == 2.0


def test_brake_looks_only_at_the_window():
    clock = Clock()
    brake = FailureBrake(threshold=3, window=60, delay=2.0, clock=clock)
    for _ in range(3):
        brake.record_failure()
    assert brake.current_delay() == 2.0
    clock.now += 61
    assert brake.current_delay() == 0
    brake.record_failure()
    assert brake.current_delay() == 0  # one new failure is not enough


def test_brake_does_not_grow_without_bound():
    brake = FailureBrake(threshold=3, window=60, delay=2.0, clock=Clock())
    for _ in range(10_000):
        brake.record_failure()
    assert len(brake.failures) <= 1000


def test_defaults_are_ten_failures_in_sixty_seconds_then_two_seconds(db_path):
    app = create_app(db_path)
    brake = app.state.login_brake
    assert (brake.threshold, brake.window, brake.delay) == (10, 60, 2.0)
    assert app.state.hash_limiter.capacity == 2


# --- the limiter ----------------------------------------------------------------------------


async def test_limiter_runs_at_most_capacity_calls_at_once():
    limiter = HashLimiter(capacity=2)
    lock = threading.Lock()
    state = {"now": 0, "most": 0, "done": 0}

    def work():
        with lock:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
        time.sleep(0.1)
        with lock:
            state["now"] -= 1
            state["done"] += 1
        return "ok"

    results = await asyncio.gather(*[limiter.run(work) for _ in range(8)])
    assert results == ["ok"] * 8
    assert state["most"] <= 2
    assert state["done"] == 8


async def test_limiter_passes_arguments_and_exceptions():
    limiter = HashLimiter(capacity=1)
    assert await limiter.run(lambda a, b=0: a + b, 1, b=2) == 3
    with pytest.raises(ZeroDivisionError):
        await limiter.run(lambda: 1 / 0)
    assert await limiter.run(lambda: "still works") == "still works"  # the slot was released


# --- in the app -----------------------------------------------------------------------------------


async def test_concurrent_logins_hash_at_most_twice_at_once(ready_client, monkeypatch):
    lock = threading.Lock()
    state = {"now": 0, "most": 0}
    real = auth.verify_password

    def slow_verify(*args, **kwargs):
        with lock:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
        time.sleep(0.08)
        try:
            return real(*args, **kwargs)
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(auth, "verify_password", slow_verify)
    app = ready_client.app
    async with asgi_client(app) as client:
        token = token_of(await client.get("/login"))
        responses = await asyncio.wait_for(
            asyncio.gather(
                *[
                    client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": "wrong-pass-1"})
                    for _ in range(8)
                ]
            ),
            timeout=30,
        )
    assert [r.status_code for r in responses] == [200] * 8
    assert state["most"] <= 2, state


class CountingLimiter:
    def __init__(self):
        self.calls = []

    async def run(self, func, *args, **kwargs):
        from starlette.concurrency import run_in_threadpool

        self.calls.append(getattr(getattr(func, "func", func), "__name__", "?"))
        return await run_in_threadpool(lambda: func(*args, **kwargs))


def test_login_setup_and_password_change_all_go_through_the_limiter(db_path):
    client = make_client(db_path)
    limiter = CountingLimiter()
    client.app.state.hash_limiter = limiter
    token = csrf_of(client, "/setup")
    client.post(
        "/setup",
        data={"csrf_token": token, "username": ADMIN, "password": PASSWORD, "password_repeat": PASSWORD, "language": "en"},
    )
    assert "create_admin" in limiter.calls
    token = csrf_of(client, "/login")
    client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD})
    assert "authenticate" in limiter.calls
    # the Bambuddy step is not needed for the password form, but the page must be reachable
    with client.app.state.sessionmaker() as s:
        from reprintbuddy import settings as st

        st.set_setting(s, "bambuddy_url", "http://bambuddy.test:8000")
        st.set_setting(s, "bambuddy_username", "reprintbuddy")
        st.set_setting(s, "bambuddy_api_key", "bb_testkey")
        st.set_setting(s, "bambuddy_review_confirmed", True)
    client.app.state.rights.status = "ok"  # not about the rights: no question to a Bambuddy (Spec 12)
    limiter.calls.clear()
    token = csrf_of(client, "/settings")
    client.post(
        "/settings/password",
        data={
            "csrf_token": token,
            "current_password": PASSWORD,
            "new_password": "brand-new-pass1",
            "new_password_repeat": "brand-new-pass1",
        },
    )
    assert "authenticate" in limiter.calls and "set_password" in limiter.calls


# --- the brake in the app ----------------------------------------------------------------------------


def _login(client, password):
    token = csrf_of(client, "/login")
    return client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": password})


@pytest.fixture
def pauses(monkeypatch):
    """The pauses the login asks for, in order, instead of really waiting for them.

    p81: these tests used to measure wall-clock time ("took at least 0.4 s", "took less than
    3 s"). Under load the four hashes alone took longer than the 3 s, and on Windows a timer may
    fire up to ~16 ms early. What matters is which pause the login asked for, so that is what is
    recorded: the login module gets a stand-in for `asyncio` whose `sleep` only writes it down.
    """
    asked: list[float] = []

    async def record(seconds):
        asked.append(seconds)

    monkeypatch.setattr("reprintbuddy.web.routes.auth.asyncio", SimpleNamespace(sleep=record))
    return asked


def test_many_failed_logins_slow_down_every_further_attempt(ready_client, pauses):
    ready_client.cookies.clear()
    brake = ready_client.app.state.login_brake
    brake.threshold, brake.delay = 3, 0.4
    ready_client.app.state.login_failure_delay = 0.5
    for _ in range(3):
        assert _login(ready_client, "wrong-password").status_code == 200
    assert pauses == [0.5, 0.5, 0.5]  # a pause per failure, no brake yet
    pauses.clear()
    response = _login(ready_client, PASSWORD)  # even the right password waits
    assert response.status_code == 303
    assert pauses == [0.4]


def test_the_brake_is_not_felt_before_the_threshold(ready_client, pauses):
    ready_client.cookies.clear()
    brake = ready_client.app.state.login_brake
    brake.threshold, brake.delay = 5, 3.0
    ready_client.app.state.login_failure_delay = 0.5
    for _ in range(4):
        _login(ready_client, "wrong-password")
    assert pauses == [0.5] * 4  # the failure pause only: the brake's 3.0 s is not among them
    _login(ready_client, "wrong-password")  # the fifth failure reaches the threshold ...
    assert 3.0 not in pauses
    _login(ready_client, "wrong-password")  # ... and from now on every attempt waits
    assert 3.0 in pauses


def test_a_successful_login_is_not_counted_as_a_failure(ready_client):
    ready_client.cookies.clear()
    brake = ready_client.app.state.login_brake
    assert _login(ready_client, PASSWORD).status_code == 303
    assert len(brake.failures) == 0
