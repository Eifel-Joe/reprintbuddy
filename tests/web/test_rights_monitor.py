"""The rights monitor: one check of the stored key per start (Spec 11.3, rights after an update)."""

import asyncio

import httpx
from conftest import REQUIRED, auth_me, mock_auth_me

from reprintbuddy.bambuddy.connection import EDITOR_LABELS, KEY_RIGHT_FOR, REQUIRED_PERMISSIONS
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
    await monitor.ensure(credentials, httpx.AsyncClient)
    assert monitor.status == "missing"
    assert monitor.notice is not None
    assert monitor.notice.missing == ("inventory:read",)


async def test_all_rights_ok(respx_mock):
    route = mock_auth_me(respx_mock)
    monitor = RightsMonitor()
    await monitor.ensure(credentials, httpx.AsyncClient)
    assert monitor.status == "ok"
    assert monitor.notice is None
    await monitor.ensure(credentials, httpx.AsyncClient)
    assert route.call_count == 1  # once per start, not once per page


async def test_admin_owner_with_all_rights_is_ok(respx_mock):
    mock_auth_me(respx_mock, is_admin=True)
    monitor = RightsMonitor()
    await monitor.ensure(credentials, httpx.AsyncClient)
    assert monitor.status == "ok"


async def test_unreachable_retries_on_next_call(respx_mock):
    route = respx_mock.get(ME).mock(side_effect=httpx.ConnectError("down"))
    monitor = RightsMonitor()
    await monitor.ensure(credentials, httpx.AsyncClient)
    assert monitor.status == "unknown"
    assert monitor.notice is None
    assert route.call_count == 1
    await monitor.ensure(credentials, httpx.AsyncClient)
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
    monitor = RightsMonitor()
    await monitor.ensure(credentials, httpx.AsyncClient)  # the first attempt waits, and fails
    await asyncio.wait_for(monitor.ensure(credentials, httpx.AsyncClient), timeout=1)  # does not wait
    assert not release.is_set()
    assert monitor.status == "unknown"
    release.set()
    await settle(monitor)
    assert monitor.status == "ok"


async def test_unauthorized_is_not_a_missing_right(respx_mock):
    mock_auth_me(respx_mock, status=401)
    monitor = RightsMonitor()
    await monitor.ensure(credentials, httpx.AsyncClient)
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
        monitor.ensure(credentials, httpx.AsyncClient),
        monitor.ensure(credentials, httpx.AsyncClient),
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
    monitor = RightsMonitor()
    await asyncio.wait_for(monitor.ensure(credentials, httpx.AsyncClient), timeout=1)
    assert monitor.status == "unknown"  # too slow: the page is shown without a notice
    # a second page does not wait for the same slow check, and does not start another one
    await asyncio.wait_for(monitor.ensure(credentials, httpx.AsyncClient), timeout=1)
    release.set()
    await settle(monitor)
    assert monitor.status == "ok"


async def test_without_setup_nothing_is_checked(respx_mock):
    route = respx_mock.get(ME).respond(200, json=auth_me())
    monitor = RightsMonitor()
    await monitor.ensure(lambda: None, httpx.AsyncClient)
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
