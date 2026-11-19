"""Spec 10.5a (1.2.2): every page shows its times in the zone of the browser that asks for it.

The page reports the browser's zone in the cookie `rb_zone` (static/js/zone.js); the server takes it for
the whole request - the page, a fetch of its regions, the answer to a form - and falls back to the zone
of the server without a valid one. The tests pin the zone of the server to Europe/Berlin (conftest).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, available_timezones

import pytest
from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from texts import en, en_html
from reprintbuddy import localtime
from reprintbuddy.models import JournalEntry
from reprintbuddy.web import zone

NOON_UTC = datetime(2026, 7, 15, 12, 0)  # 08:00 in New York, 14:00 in Berlin


def journal_row_at_noon(client) -> int:
    with sm_of(client).begin() as s:
        a = make_article(s, name="Template A")
        s.add(JournalEntry(at=NOON_UTC, booked_at=NOON_UTC, actor="user", kind="sale", qty=1, article_id=a.id))
        return a.id


@pytest.fixture
def new_york(ready_client):
    ready_client.cookies.set(zone.COOKIE, "America/New_York")
    yield ready_client
    ready_client.cookies.delete(zone.COOKIE)


def test_a_page_is_shown_in_the_zone_of_the_browser(new_york):
    journal_row_at_noon(new_york)
    page = new_york.get("/journal").text
    assert "2026-07-15 08:00" in page and "2026-07-15 14:00" not in page
    assert 'data-zone="America/New_York"' in page  # the zone the page was computed in, for zone.js


def test_without_a_cookie_the_zone_of_the_server(ready_client):
    journal_row_at_noon(ready_client)
    page = ready_client.get("/journal").text
    assert "2026-07-15 14:00" in page
    assert 'data-zone="Europe/Berlin"' in page


@pytest.mark.parametrize(
    "value",
    [
        "Mars/Olympus_Mons",  # no zone
        "../../etc/passwd",  # a path
        "/usr/share/zoneinfo/Europe/Berlin",
        "Europe/../Europe/Berlin",
        "europe/berlin",  # the list is exact
        "localtime",
        "x" * 65,
        "",
    ],
)
def test_an_invalid_cookie_counts_as_none_quietly(ready_client, caplog, value):
    """No error, no line in the log, the value nowhere in the page (Spec 10.5a "Sicherheit")."""
    journal_row_at_noon(ready_client)
    ready_client.cookies.set(zone.COOKIE, value)
    try:
        with caplog.at_level(logging.DEBUG):
            response = ready_client.get("/journal")
    finally:
        ready_client.cookies.delete(zone.COOKIE)
    assert response.status_code == 200
    assert "2026-07-15 14:00" in response.text and 'data-zone="Europe/Berlin"' in response.text
    if value:
        assert value not in response.text
        assert all(value not in record.getMessage() for record in caplog.records)
    assert not [r for r in caplog.records if r.name.startswith("reprintbuddy.web.zone")]


def test_only_names_of_the_list_up_to_64_characters(monkeypatch):
    long_name = "Etc/" + "x" * 61  # 65 characters
    monkeypatch.setattr(zone, "KNOWN_ZONES", zone.KNOWN_ZONES | {long_name})
    assert zone.reported_zone(long_name) is None
    assert zone.reported_zone("Asia/Kolkata") == ZoneInfo("Asia/Kolkata")
    assert zone.reported_zone(None) is None


def test_the_list_of_zones_is_built_once_at_start(monkeypatch):
    assert zone.KNOWN_ZONES == zone.known_zones(available_timezones())

    def not_again():
        raise AssertionError("the list is read again for a request")

    monkeypatch.setattr("zoneinfo.available_timezones", not_again)
    assert zone.reported_zone("Europe/Vienna") == ZoneInfo("Europe/Vienna")


def test_entries_of_the_system_are_no_zones():
    """Debian and Ubuntu put `/usr/share/zoneinfo/localtime -> /etc/localtime` into the zone folder, and
    `available_timezones()` lists every zone file it finds there: "localtime" was taken as a zone (CI on
    Linux failed the test above). It and "posixrules" are no IANA names (Spec 10.5a "Sicherheit")."""
    listed = ["Europe/Berlin", "localtime", "posixrules", "Etc/UTC"]
    assert zone.known_zones(listed) == frozenset({"Europe/Berlin", "Etc/UTC"})
    assert "localtime" not in zone.KNOWN_ZONES and "posixrules" not in zone.KNOWN_ZONES


def test_a_zone_folder_like_debians_lists_no_localtime(tmp_path):
    """The folder of a Debian system, rebuilt on any machine: a zone file named `localtime` next to a real one."""
    import zoneinfo
    from importlib import resources

    folder = tmp_path / "zoneinfo"
    (folder / "Etc").mkdir(parents=True)
    utc = resources.files("tzdata").joinpath("zoneinfo/Etc/UTC").read_bytes()
    (folder / "Etc" / "UTC").write_bytes(utc)
    (folder / "localtime").write_bytes(utc)
    zoneinfo.reset_tzpath(to=[str(folder)])
    try:
        listed = zoneinfo.available_timezones()
    finally:
        zoneinfo.reset_tzpath()
    assert "localtime" in listed  # what the system offers
    assert "localtime" not in zone.known_zones(listed) and "Etc/UTC" in zone.known_zones(listed)


def test_a_region_is_shown_in_the_same_zone_as_the_page(new_york):
    """Spec 10.5.3: a region comes from the same request as the whole page, so in the same zone."""
    journal_row_at_noon(new_york)
    response = new_york.get("/journal", headers={"X-Live-Regions": "journal"})
    assert response.headers["X-Live-Regions"] == "journal"
    assert "2026-07-15 08:00" in response.text and "2026-07-15 14:00" not in response.text


def test_the_answer_to_a_form_is_shown_in_the_zone_of_the_browser(new_york):
    a = journal_row_at_noon(new_york)
    data = {
        "csrf_token": csrf_of(new_york, "/bookings"),
        "form_token": form_token_of(new_york, "/bookings", "/bookings/sale"),
        "article_id": a, "qty": "1", "booked_on": "2999-01-01",
    }
    response = new_york.post("/bookings/sale", data=data)
    assert response.status_code == 200 and en_html("bookings.date_future") in response.text
    assert "2026-07-15 08:00" in response.text
    assert 'data-zone="America/New_York"' in response.text
    assert "data-form-answer" in response.text  # zone.js never loads such a page again (a second booking)


def test_a_page_for_a_get_is_no_form_answer(new_york):
    assert "data-form-answer" not in new_york.get("/bookings").text


def test_the_sign_in_page_carries_the_zone_too(admin_client):
    """The page reports the zone from the very first page on (Spec 10.5a): the bare pages have it too."""
    admin_client.cookies.clear()  # signed out
    admin_client.cookies.set(zone.COOKIE, "Asia/Tokyo")
    response = admin_client.get("/login")
    assert response.status_code == 200 and 'data-zone="Asia/Tokyo"' in response.text


# ---- the days of a booking (Spec 10.5a "Tage beim Buchen"): a server in UTC, a browser in Berlin -------

LATE_EVENING_UTC = datetime(2026, 11, 18, 23, 30)  # 19 November, 00:30 in Berlin


@pytest.fixture
def utc_server_at_half_past_midnight_in_berlin(ready_client, monkeypatch):
    from reprintbuddy.web.routes import bookings, orders

    monkeypatch.setattr(localtime, "SERVER_ZONE", ZoneInfo("UTC"))
    monkeypatch.setattr(bookings, "utcnow", lambda: LATE_EVENING_UTC)
    monkeypatch.setattr(orders, "utcnow", lambda: LATE_EVENING_UTC)
    return ready_client


def post_booking(client, action, **fields):
    data = {
        "csrf_token": csrf_of(client, "/bookings"),
        "form_token": form_token_of(client, "/bookings", f"/bookings/{action}"),
        **fields,
    }
    return client.post(f"/bookings/{action}", data=data)


def test_today_in_the_date_field_is_the_browsers_day(utc_server_at_half_past_midnight_in_berlin):
    """Root of the side finding: on a server in UTC the date field offered the 18th as "today" and as the
    latest day until 1 o'clock in Berlin (2 in summer), and refused the 19th as a day in the future."""
    client = utc_server_at_half_past_midnight_in_berlin
    assert 'value="2026-11-18" max="2026-11-18"' in client.get("/bookings").text  # no cookie: the server's day
    client.cookies.set(zone.COOKIE, "Europe/Berlin")
    try:
        assert 'value="2026-11-19" max="2026-11-19"' in client.get("/bookings").text
        with sm_of(client).begin() as s:
            a = make_article(s, name="Template A", stock=5).id
        response = post_booking(client, "sale", article_id=a, qty="1", booked_on="2026-11-19")
        assert response.status_code == 303
        with sm_of(client).begin() as s:
            row = s.scalars(select(JournalEntry)).one()
            assert row.booked_at == row.at  # today: counts when it is saved
    finally:
        client.cookies.delete(zone.COOKIE)


def test_the_19th_is_a_future_day_for_the_server_alone(utc_server_at_half_past_midnight_in_berlin):
    client = utc_server_at_half_past_midnight_in_berlin
    with sm_of(client).begin() as s:
        a = make_article(s, name="Template A", stock=5).id
    response = post_booking(client, "sale", article_id=a, qty="1", booked_on="2026-11-19")
    assert response.status_code == 200 and en_html("bookings.date_future") in response.text


def test_an_inventory_dated_back_holds_until_the_end_of_the_browsers_day(utc_server_at_half_past_midnight_in_berlin):
    client = utc_server_at_half_past_midnight_in_berlin
    client.cookies.set(zone.COOKIE, "Europe/Berlin")
    try:
        with sm_of(client).begin() as s:
            a = make_article(s, name="Template A", stock=5).id
        response = post_booking(client, "inventory", article_id=a, field="stock", counted="4", booked_on="2026-11-17")
        assert response.status_code == 303
        with sm_of(client).begin() as s:
            row = s.scalars(select(JournalEntry)).one()
        assert row.booked_at == datetime(2026, 11, 17, 22, 59, 59, 999999)  # midnight in Berlin, UTC+1
        page = client.get("/bookings").text
        assert en("bookings.inventory_done_then", item="Template A", counted=4, diff="-1", day="2026-11-17", then=5) in page
    finally:
        client.cookies.delete(zone.COOKIE)


def test_the_order_date_is_today_in_the_browser(utc_server_at_half_past_midnight_in_berlin):
    client = utc_server_at_half_past_midnight_in_berlin
    assert 'name="order_date" type="date" value="2026-11-18"' in client.get("/orders/new").text
    client.cookies.set(zone.COOKIE, "Europe/Berlin")
    try:
        assert 'name="order_date" type="date" value="2026-11-19"' in client.get("/orders/new").text
    finally:
        client.cookies.delete(zone.COOKIE)


def test_the_zone_ends_with_the_request(new_york):
    new_york.get("/journal")
    assert localtime.browser_zone() is None
    assert localtime.local_date(datetime(2026, 7, 15, 23, 0)) == date(2026, 7, 16)  # Berlin again
