"""The stand-in for Bambuddy of the end-to-end run (tests/system/fakebambuddy.py), read by the production client:
the setup accepts it and the readings in the background get answers they can parse."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import fakebambuddy
from reprintbuddy.bambuddy.client import HttpBambuddyClient
from reprintbuddy.bambuddy.connection import REQUIRED_PERMISSIONS, check_connection

KEY = "e2e-key-0123456789abcdef"


@pytest.fixture
def server(tmp_path: Path):
    fake = fakebambuddy.FakeBambuddy(KEY, log=tmp_path / "bambuddy.log")
    fake.start(port=0)
    yield fake
    fake.stop()


def _client(server, key: str = KEY) -> HttpBambuddyClient:
    @asynccontextmanager
    async def factory():
        async with httpx.AsyncClient(trust_env=False) as client:
            yield client

    return HttpBambuddyClient(server.url, key, factory)


async def test_the_setup_check_passes_with_every_required_right_and_no_admin(server) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        check = await check_connection(server.url, KEY, client)
    assert check.ok and check.reason == "ok"
    assert set(REQUIRED_PERMISSIONS) <= check.granted
    assert not check.owner_is_admin
    assert not check.can_live  # no live channel: it would open a websocket the stand-in does not have
    assert server.unexpected == []


async def test_a_wrong_key_is_refused(server) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        check = await check_connection(server.url, "another-key-0123456789", client)
    assert check.reason == "unauthorized"


async def test_the_readings_in_the_background_parse(server) -> None:
    client = _client(server)
    assert await client.list_printers() == []
    assert await client.printer_statuses([]) == {}
    assert await client.list_queue() == []
    assert await client.list_all_items() == []
    assert await client.spool_mode() == "builtin"
    assert await client.list_spools() == []
    assert await client.shortest_first() is False
    await client.print_defaults()
    assert await client.list_folders() == []
    assert await client.list_files(None) == []
    assert server.unexpected == [] and server.unknown == []


async def test_a_write_is_refused_and_recorded_and_an_unknown_reading_is_noted(server) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        done = await client.post(f"{server.url}/api/v1/queue/", headers={"X-API-Key": KEY}, json={})
        assert done.status_code == 405
        missing = await client.get(f"{server.url}/api/v1/archives/", headers={"X-API-Key": KEY})
        assert missing.status_code == 404
    assert server.unexpected == ["POST /api/v1/queue/"]
    assert server.unknown == ["GET /api/v1/archives/"]
    text = server.log.read_text(encoding="utf-8")
    assert "UNEXPECTED POST /api/v1/queue/" in text and "UNKNOWN GET /api/v1/archives/" in text
    assert KEY not in text
