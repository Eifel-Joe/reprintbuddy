"""Requests that overlap: no lost changes, no locked database, no frozen pool."""

import asyncio
import re
import time

import httpx
import pytest
from conftest import ADMIN, PASSWORD

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
    return httpx.Response(200, json=[])


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
                    "bambuddy_url": "http://BAMBUDDY.test:8000",
                    "bambuddy_username": "other-user",
                    "bambuddy_api_key": "",
                    "bambuddy_review_confirmed": "on",
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
