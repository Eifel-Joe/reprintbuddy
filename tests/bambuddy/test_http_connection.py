"""The connection check and `ping` inside `HttpBambuddyClient` (B2 Task 2, Decisions 2 and 5)."""

import asyncio
from contextlib import nullcontext

import httpx
import pytest
from fixtures import API, KEY, URL, client, load

from reprintbuddy.bambuddy.client import HttpBambuddyClient
from reprintbuddy.bambuddy.connection import check_connection

ME = f"{API}/auth/me"


async def test_check_connection_with_recorded_answer(respx_mock):
    respx_mock.get(ME).respond(200, json=load("auth_me.json"))
    result = await client().check_connection()
    assert result.ok is True
    assert result.owner == "reprintbuddy"
    assert result.owner_is_admin is False


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"}), id="html"),
        pytest.param(httpx.Response(307, headers={"Location": f"{URL}/login"}), id="redirect"),
        pytest.param(httpx.Response(404), id="404"),
    ],
)
async def test_unexpected_answer_is_not_bambuddy(respx_mock, answer):
    respx_mock.get(ME).mock(return_value=answer)
    result = await client().check_connection()
    assert result.ok is False
    assert result.reason == "not_bambuddy"
    assert result.reason != "unreachable"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (httpx.Response(401, json={"detail": "no"}), "unauthorized"),
        (httpx.Response(403, json={"detail": "no"}), "forbidden"),
        (httpx.Response(500), "unreachable"),
        (httpx.Response(422, json={"detail": "x"}), "not_bambuddy"),
        (httpx.Response(204), "not_bambuddy"),
    ],
)
async def test_status_mapping(respx_mock, answer, reason):
    respx_mock.get(ME).mock(return_value=answer)
    assert (await client().check_connection()).reason == reason


@pytest.mark.parametrize("error", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
async def test_network_error_is_unreachable(respx_mock, error):
    respx_mock.get(ME).mock(side_effect=error)
    assert (await client().check_connection()).reason == "unreachable"


async def test_ping_true_when_reachable(respx_mock):
    respx_mock.get(ME).respond(200, json=load("auth_me.json"))
    assert await client().ping() is True


async def test_ping_true_without_queue_create(respx_mock):
    body = load("auth_me.json")
    body["permissions"] = [p for p in body["permissions"] if p != "queue:create"]
    respx_mock.get(ME).respond(200, json=body)
    assert await client().ping() is True


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(httpx.Response(401, json={"detail": "no"}), id="401"),
        pytest.param(httpx.Response(403, json={"detail": "no"}), id="403"),
        pytest.param(httpx.Response(500), id="500"),
        pytest.param(httpx.ConnectError("x"), id="connect-error"),
        pytest.param(
            httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"}), id="html"
        ),
    ],
)
async def test_ping_false(respx_mock, answer):
    if isinstance(answer, Exception):
        respx_mock.get(ME).mock(side_effect=answer)
    else:
        respx_mock.get(ME).mock(return_value=answer)
    assert await client().ping() is False


async def test_wrapper_keeps_the_callers_client_open(respx_mock):
    respx_mock.get(ME).respond(200, json=load("auth_me.json"))
    async with httpx.AsyncClient() as c:
        result = await check_connection(URL, KEY, c)
        assert result.ok is True
        assert not c.is_closed


async def test_201_is_not_bambuddy(respx_mock):
    respx_mock.get(ME).respond(201, json=load("auth_me.json"))
    assert (await client().check_connection()).reason == "not_bambuddy"


async def test_ping_false_for_non_ascii_key(respx_mock):
    respx_mock.get(ME).respond(200, json=load("auth_me.json"))
    c = HttpBambuddyClient(URL, "schlüssel-ä", lambda: httpx.AsyncClient())
    assert await c.ping() is False


async def test_ping_false_for_closed_client():
    closed = httpx.AsyncClient()
    await closed.aclose()
    c = HttpBambuddyClient(URL, KEY, lambda: nullcontext(closed))
    assert await c.ping() is False


async def test_cancelled_ping_still_raises(respx_mock):
    async def slow(request):
        await asyncio.sleep(30)

    respx_mock.get(ME).mock(side_effect=slow)
    task = asyncio.ensure_future(client().ping())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
