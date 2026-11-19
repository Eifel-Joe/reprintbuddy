"""p183, Spec 10.5a: without a zone from the browser, times are shown in the time zone of the server.

The other tests pin a zone of the server (`conftest.fixed_display_zone`) so that they read the same on
every machine; these set the default back. The zone of the browser: tests/web/test_browser_zone.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from reprintbuddy import localtime
from reprintbuddy.web import formatting


@pytest.fixture
def server_zone(monkeypatch):
    monkeypatch.setattr(localtime, "SERVER_ZONE", None)


def test_the_default_is_the_zone_of_the_server():
    """The module itself, not the pinned value of the tests: a process of its own."""
    script = "from reprintbuddy import localtime\nprint(localtime.SERVER_ZONE, localtime.browser_zone())\n"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["None", "None"]  # `astimezone(None)`: the zone the machine is set to


@pytest.mark.usefixtures("server_zone")
def test_a_stored_time_is_shown_in_the_zone_of_the_server():
    moment = datetime(2026, 7, 15, 12, 0)  # stored naive, meaning UTC
    assert formatting.to_local(moment) == moment.replace(tzinfo=UTC).astimezone()


@pytest.mark.skipif(os.name != "posix", reason="the TZ variable sets the zone of a process on POSIX only")
@pytest.mark.parametrize(
    ("zone", "winter", "summer"),
    [("America/New_York", "2026-01-15 07:00", "2026-07-15 08:00"), ("UTC", "2026-01-15 12:00", "2026-07-15 12:00")],
)
def test_a_server_elsewhere_shows_its_own_time(zone, winter, summer):
    script = (
        "from datetime import datetime\n"
        "from reprintbuddy.web.formatting import format_local\n"
        "print(format_local(datetime(2026, 1, 15, 12, 0), 'en'))\n"
        "print(format_local(datetime(2026, 7, 15, 12, 0), 'en'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, env={**os.environ, "TZ": zone}
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\n")[:2] == [winter, summer]


@pytest.mark.usefixtures("server_zone")
def test_the_date_on_a_label_is_the_day_of_the_server_without_a_browser_zone():
    from reprintbuddy.web import labels_view

    shown = labels_view.today_text("en")
    assert shown == datetime.now(UTC).astimezone().date().isoformat()


@pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])  # UTC+14 and UTC-11
def test_the_date_on_a_label_is_the_day_of_the_browser(zone):
    """Spec 8.5, 10.5a: printed from a browser, the label carries today in that browser's zone. The two
    zones are 25 hours apart, so at any moment at least one of them has another day than the server."""
    from reprintbuddy.web import labels_view

    with localtime.shown_in(ZoneInfo(zone)):
        shown = labels_view.today_text("en")
    assert shown == datetime.now(ZoneInfo(zone)).date().isoformat()
