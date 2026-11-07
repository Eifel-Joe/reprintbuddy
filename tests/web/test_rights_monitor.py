"""The rights monitor: one check of the stored key per start (Spec 11.3, rights after an update)."""

import asyncio
import logging

import httpx
from conftest import REQUIRED, auth_me, http_client_factory, mock_auth_me

from reprintbuddy.bambuddy.connection import EDITOR_LABELS, KEY_RIGHT_FOR, REQUIRED_PERMISSIONS, ConnectionCheck
from reprintbuddy.web.i18n import load_locales
from reprintbuddy.web.rights import FIRST_WAIT_SECONDS, RightsMonitor

URL = "http://bambuddy.test:8000"
ME = f"{URL}/api/v1/auth/me"


def credentials():
    return URL, "bb_testkey"


async def settle(monitor: RightsMonitor) -> None:
    """Let the background check of a later attempt finish."""
    task = monitor._task
    if task is not None:
        await asyncio.wait({task}, timeout=5)


async def test_missing_right_sets_notice(respx_mock):
    mock_auth_me(respx_mock, permissions=[p for p in REQUIRED if p != "inventory:read"])
    monitor = RightsMonitor()
    await monitor.ensure(credentials, http_client_factory)
    assert monitor.status == "missing"
    assert monitor.notice is not None
    assert monitor.notice.missing == ("inventory:read",)


async def test_all_rights_ok(respx_mock):
    route = mock_auth_me(respx_mock)
    monitor = RightsMonitor()
    await monitor.ensure(credentials, http_client_factory)
    assert monitor.status == "ok"
    assert monitor.notice is None
    await monitor.ensure(credentials, http_client_factory)
    assert route.call_count == 1  # once per start, not once per page


async def test_admin_owner_with_all_rights_is_ok(respx_mock):
    mock_auth_me(respx_mock, is_admin=True)
    monitor = RightsMonitor()
    await monitor.ensure(credentials, http_client_factory)
    assert monitor.status == "ok"


async def test_unreachable_retries_on_next_call(respx_mock):
    route = respx_mock.get(ME).mock(side_effect=httpx.ConnectError("down"))
    monitor = RightsMonitor()
    await monitor.ensure(credentials, http_client_factory)
    assert monitor.status == "unknown"
    assert monitor.notice is None
    assert route.call_count == 1
    await monitor.ensure(credentials, http_client_factory)
    await settle(monitor)
    assert route.call_count == 2
    assert monitor.attempts == 2


async def test_later_attempts_do_not_wait(respx_mock):
    release = asyncio.Event()
    calls = []

    async def answer(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        await release.wait()
        return httpx.Response(200, json=auth_me())

    respx_mock.get(ME).mock(side_effect=answer)
    # Both clients are built before the clock runs (see test_first_attempt_waits_at_most_the_first_wait).
    clients = iter([httpx.AsyncClient(), httpx.AsyncClient()])
    monitor = RightsMonitor()
    await monitor.ensure(credentials, lambda: next(clients))  # the first attempt waits, and fails
    await asyncio.wait_for(monitor.ensure(credentials, lambda: next(clients)), timeout=1)  # does not wait
    assert not release.is_set()
    assert monitor.status == "unknown"
    release.set()
    await settle(monitor)
    assert monitor.status == "ok"


async def test_unauthorized_is_not_a_missing_right(respx_mock):
    mock_auth_me(respx_mock, status=401)
    monitor = RightsMonitor()
    await monitor.ensure(credentials, http_client_factory)
    assert monitor.status == "unknown"
    assert monitor.notice is None


async def test_concurrent_ensure_runs_one_check(respx_mock):
    release = asyncio.Event()

    async def answer(request):
        await release.wait()
        return httpx.Response(200, json=auth_me())

    route = respx_mock.get(ME).mock(side_effect=answer)
    monitor = RightsMonitor()

    async def open_the_gate():
        await asyncio.sleep(0.05)
        release.set()

    await asyncio.gather(
        monitor.ensure(credentials, http_client_factory),
        monitor.ensure(credentials, http_client_factory),
        open_the_gate(),
    )
    assert route.call_count == 1
    assert monitor.status == "ok"


async def test_first_attempt_waits_at_most_the_first_wait(respx_mock, monkeypatch):
    monkeypatch.setattr("reprintbuddy.web.rights.FIRST_WAIT_SECONDS", 0.05)
    release = asyncio.Event()

    async def answer(request):
        await release.wait()
        return httpx.Response(200, json=auth_me())

    respx_mock.get(ME).mock(side_effect=answer)
    # The client is built before the clock runs. Building one loads the TLS certificates
    # synchronously, which takes over a second on a busy Windows machine and would block the
    # loop inside the measured time; the test is about the wait, not about building a client.
    # Only one check runs, so the factory hands out this one client once.
    client = httpx.AsyncClient()
    monitor = RightsMonitor()
    await asyncio.wait_for(monitor.ensure(credentials, lambda: client), timeout=1)
    assert monitor.status == "unknown"  # too slow: the page is shown without a notice
    # a second page does not wait for the same slow check, and does not start another one
    await asyncio.wait_for(monitor.ensure(credentials, lambda: client), timeout=1)
    assert monitor.attempts == 1
    release.set()
    await settle(monitor)
    assert monitor.status == "ok"


async def test_a_late_old_check_does_not_overwrite_a_newer_verdict(respx_mock, monkeypatch):
    """M2 of the review: a slow check made with the old key must not undo what a save just found."""
    monkeypatch.setattr("reprintbuddy.web.rights.FIRST_WAIT_SECONDS", 0.05)
    release = asyncio.Event()

    async def answer(request):
        await release.wait()
        return httpx.Response(200, json=auth_me(permissions=["printers:read"]))  # many rights missing

    respx_mock.get(ME).mock(side_effect=answer)
    monitor = RightsMonitor()
    await monitor.ensure(credentials, http_client_factory)  # gives up waiting, the check goes on
    assert monitor.status == "unknown"
    monitor.record(ConnectionCheck(True, "ok", owner="reprintbuddy"))  # a new key was saved: all rights
    release.set()
    await settle(monitor)
    assert monitor.status == "ok" and monitor.notice is None


async def test_a_verdict_taken_before_the_check_starts_running_wins_too(respx_mock):
    """The credentials were read in `ensure`: a verdict taken right after is newer than they are."""
    respx_mock.get(ME).respond(200, json=auth_me(permissions=["printers:read"]))
    monitor = RightsMonitor()
    task = asyncio.create_task(monitor.ensure(credentials, http_client_factory))
    await asyncio.sleep(0)  # `ensure` has read the credentials and created the check, which has not run yet
    monitor.record(ConnectionCheck(True, "ok", owner="reprintbuddy"))
    await task
    await settle(monitor)
    assert monitor.status == "ok"


async def test_a_failing_check_is_logged_without_secrets(caplog):
    """M3 of the review: a programming error must not make the monitor silently useless."""

    def broken_factory():
        raise RuntimeError("http://bambuddy.test:8000 bb_SECRETKEY_999")

    monitor = RightsMonitor()
    with caplog.at_level(logging.WARNING, logger="reprintbuddy.rights"):
        await monitor.ensure(lambda: (URL, "bb_SECRETKEY_999"), broken_factory)
    assert monitor.status == "unknown"
    assert "rights check failed" in caplog.text and "RuntimeError" in caplog.text
    assert "bb_SECRETKEY_999" not in caplog.text and "bambuddy.test" not in caplog.text


async def test_without_setup_nothing_is_checked(respx_mock):
    route = respx_mock.get(ME).respond(200, json=auth_me())
    monitor = RightsMonitor()
    await monitor.ensure(lambda: None, http_client_factory)
    assert route.call_count == 0
    assert monitor.status == "unknown"
    assert monitor.attempts == 0


def test_first_wait_is_three_seconds():
    assert FIRST_WAIT_SECONDS == 3.0


def test_every_required_right_has_label_place_and_purpose():
    """A future version that adds a required right must bring label, place and purpose with it."""
    locales = load_locales()
    for code in REQUIRED_PERMISSIONS:
        assert EDITOR_LABELS.get(code), code
        assert KEY_RIGHT_FOR.get(code) in ("status", "queue"), code
        for lang, texts in locales.items():
            assert texts.get(f"permission.{code}"), (lang, code)
