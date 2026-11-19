"""The day of an inventory dated back, shown in another zone than it was booked in (Spec 10.5a, midnight rule).

A count dated back is stored at the end of its day in the zone it was booked in (p260). Up to 1.2.1 that was
the zone of the server, on the live instance UTC. Shown in a browser in Berlin, the end of 18 November in
UTC is 00:59 on the 19th: without the rule the journal and the messages would name the 19th.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import sm_of
from factories import make_article
from reprintbuddy import localtime
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.models import JournalEntry
from reprintbuddy.web import forms, zone

END_OF_18_NOV_UTC = datetime(2026, 11, 18, 23, 59, 59, 999999)
END_OF_18_NOV_BERLIN = datetime(2026, 11, 18, 22, 59, 59, 999999)
SAVED = datetime(2026, 11, 19, 9, 0)


def counted_back(client, booked_at) -> None:
    with sm_of(client).begin() as s:
        a = make_article(s, name="Template A")
        s.add(JournalEntry(
            at=SAVED, booked_at=booked_at, actor="user", kind="inventory", qty=0, article_id=a.id,
            details={"field": "stock", "counted": 4, "then": [4, 4]},
        ))


@pytest.mark.parametrize(
    ("stored", "browser"),
    [
        (END_OF_18_NOV_UTC, "Europe/Berlin"),  # booked on a server in UTC, read in Berlin
        (END_OF_18_NOV_UTC, None),  # the same without a cookie: the server is in Berlin now (conftest)
        (END_OF_18_NOV_BERLIN, "America/New_York"),  # booked in Berlin, read in New York
        (END_OF_18_NOV_BERLIN, "Asia/Tokyo"),
    ],
)
def test_the_journal_shows_the_day_that_was_booked(ready_client, stored, browser):
    counted_back(ready_client, stored)
    if browser:
        ready_client.cookies.set(zone.COOKIE, browser)
    try:
        page = ready_client.get("/journal").text
    finally:
        ready_client.cookies.delete(zone.COOKIE)
    assert '<td class="nowrap">2026-11-18<span class="col-note">' in page


@pytest.mark.parametrize("stored", [END_OF_18_NOV_UTC, END_OF_18_NOV_BERLIN])
@pytest.mark.parametrize("browser", ["Europe/Berlin", "UTC", "America/Los_Angeles", "Asia/Kolkata"])
def test_a_message_names_the_day_that_was_booked(stored, browser):
    """`bookings.counted_later` and `bookings.later_inventory` carry the moment of the count (p261)."""
    error = ValidationError("bookings.counted_later", item="Template A", at=stored)
    with localtime.shown_in(ZoneInfo(browser)):
        text = forms.error_text("en", error)
    assert "2026-11-18" in text and "2026-11-19" not in text and "2026-11-17" not in text


def test_a_message_about_a_moment_names_its_own_day():
    """A count of "today" holds at its moment: 23:30 UTC is the 19th in Berlin, no rule applies."""
    error = ValidationError("bookings.counted_later", item="Template A", at=datetime(2026, 11, 18, 23, 30))
    assert "2026-11-19" in forms.error_text("en", error)
