"""Settings → General says in which zone this browser sees the times (Spec 10.5a)."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from texts import de, en
from reprintbuddy import localtime
from reprintbuddy import settings as st
from reprintbuddy.web import zone


def line(page: str) -> str:
    found = re.search(r'<p class="muted" id="time-zone">(.*?)</p>', page)
    assert found, "no zone line"
    return found.group(1)


def test_the_zone_of_this_browser(ready_client):
    ready_client.cookies.set(zone.COOKIE, "America/New_York")
    try:
        page = ready_client.get("/settings?tab=general").text
    finally:
        ready_client.cookies.delete(zone.COOKIE)
    assert line(page) == en("settings.zone_browser", zone="America/New_York")


def test_without_a_valid_cookie_the_zone_of_the_server(ready_client, monkeypatch):
    monkeypatch.setattr(localtime, "SERVER_ZONE", ZoneInfo("UTC"))
    assert line(ready_client.get("/settings").text) == en("settings.zone_server", zone="UTC")
    ready_client.cookies.set(zone.COOKIE, "Not/A_Zone")
    try:
        page = ready_client.get("/settings?tab=general").text
    finally:
        ready_client.cookies.delete(zone.COOKIE)
    assert line(page) == en("settings.zone_server", zone="UTC") and "Not/A_Zone" not in page


def test_in_german(ready_client):
    with ready_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    ready_client.cookies.set(zone.COOKIE, "Europe/Berlin")
    try:
        page = ready_client.get("/settings").text
    finally:
        ready_client.cookies.delete(zone.COOKIE)
    assert line(page) == de("settings.zone_browser", zone="Europe/Berlin")
    assert line(page) == "Zeiten erscheinen in der Zeitzone dieses Browsers: Europe/Berlin"


def test_only_on_the_general_tab(ready_client):
    assert 'id="time-zone"' not in ready_client.get("/settings?tab=account").text
