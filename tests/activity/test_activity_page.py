"""The tab "Log" under Settings (Spec 10.7, "Anzeige"): newest first, filters by group and by who with
counts, pages like the journal, the address only for sign-in, live, texts in both languages."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from conftest import sm_of
from reprintbuddy import settings as st
from reprintbuddy.models import ActivityEntry
from reprintbuddy.web.activity_view import PAGE_SIZE

PAGE = "/settings?tab=log"


def add(client, *entries: ActivityEntry) -> None:
    with sm_of(client).begin() as s:
        s.add_all(entries)


def table_rows(html: str) -> list[str]:
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    return re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S) if body else []


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def test_the_tab_is_there_and_empty_says_where_the_bookings_are(ready_client):
    with sm_of(ready_client).begin() as s:
        s.query(ActivityEntry).delete()
    html = ready_client.get(PAGE).text
    assert 'href="/settings?tab=log"' in html
    assert "Nothing has been logged yet" in html


def test_newest_first_with_group_event_and_who(ready_client):
    t0 = datetime(2026, 11, 9, 8, 0)
    with sm_of(ready_client).begin() as s:
        s.query(ActivityEntry).delete()
    add(
        ready_client,
        ActivityEntry(at=t0, area="login", kind="login", actor="user", address="192.0.2.7"),
        ActivityEntry(at=t0 + timedelta(minutes=5), area="settings", kind="settings_changed", actor="user",
                      address="192.0.2.7",
                      details={"settings": [{"setting": "poll_minutes", "old": 15, "new": 10},
                                            {"setting": "automation", "old": False, "new": True},
                                            {"setting": "bambuddy_key", "change": "changed"}]}),
        ActivityEntry(at=t0 + timedelta(minutes=9), area="ops", kind="started", actor="system", via=None,
                      details={"version": "0.3.13"}),
        ActivityEntry(at=t0 + timedelta(minutes=10), area="settings", kind="settings_changed", actor="user", via="cli",
                      details={"settings": [{"setting": "port", "old": 8080, "new": 8090}]}),
    )
    rows = [text_of(row) for row in table_rows(ready_client.get(PAGE).text)]
    assert len(rows) == 4
    assert "Port: 8080 → 8090" in rows[0] and "User (command on the server)" in rows[0]
    assert "Reprintbuddy started (version 0.3.13)" in rows[1] and "System" in rows[1]
    assert "Poll interval (minutes): 15 → 10" in rows[2]
    assert "Automation (main switch): off → on" in rows[2]
    assert "Bambuddy API key: changed" in rows[2]
    assert "192.0.2.7" not in rows[2]  # the address only for sign-in
    assert "Signed in" in rows[3] and "192.0.2.7" in rows[3]


def test_filters_by_group_and_by_who_with_counts(ready_client):
    with sm_of(ready_client).begin() as s:
        s.query(ActivityEntry).delete()
    add(
        ready_client,
        ActivityEntry(area="login", kind="login", actor="user"),
        ActivityEntry(area="login", kind="logout", actor="user"),
        ActivityEntry(area="ops", kind="started", actor="system", details={"version": "1"}),
        ActivityEntry(area="bambuddy", kind="bambuddy_connection", actor="bambuddy", details={"state": "unreachable"}),
    )
    html = ready_client.get(PAGE).text
    assert re.search(r'href="/settings\?tab=log&amp;area=login"[^>]*>Sign-in <span class="count">2</span>', html)
    assert re.search(r'href="/settings\?tab=log&amp;actor=bambuddy"[^>]*>Bambuddy <span class="count">1</span>', html)
    login_only = table_rows(ready_client.get(PAGE + "&area=login").text)
    assert len(login_only) == 2
    system_only = [text_of(row) for row in table_rows(ready_client.get(PAGE + "&actor=system").text)]
    assert len(system_only) == 1 and "Reprintbuddy started" in system_only[0]
    # Every group has its button, also with nothing in it.
    assert re.search(r'href="/settings\?tab=log&amp;area=labels"[^>]*>Labels <span class="count">0</span>', html)
    nothing = ready_client.get(PAGE + "&area=labels").text
    assert "Nothing logged for this filter." in nothing
    assert ready_client.get(PAGE + "&area=<script>&actor=x&page=-1").status_code == 200  # unknown: no filter


def test_pages_like_the_journal(ready_client):
    with sm_of(ready_client).begin() as s:
        s.query(ActivityEntry).delete()
    t0 = datetime(2026, 11, 9, 8, 0)
    add(ready_client, *[ActivityEntry(at=t0 + timedelta(seconds=i), area="login", kind="login", actor="user")
                        for i in range(PAGE_SIZE + 3)])
    first = ready_client.get(PAGE).text
    assert len(table_rows(first)) == PAGE_SIZE
    assert 'href="/settings?tab=log&amp;page=2"' in first
    second = ready_client.get(PAGE + "&page=2").text
    assert len(table_rows(second)) == 3
    assert 'href="/settings?tab=log"' in second


def test_the_list_follows_live(ready_client):
    html = ready_client.get(PAGE).text
    assert 'data-live-region="activity-log" data-live-topics="activity"' in html


def test_unknown_codes_of_another_version_show_as_unknown(ready_client):
    add(ready_client, ActivityEntry(area="ops", kind="update_result", actor="system",
                                    details={"from": "v1", "to": "v2", "result": "exploded"}))
    html = ready_client.get(PAGE).text
    assert "unknown" in html


def test_in_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
        s.query(ActivityEntry).delete()
    add(ready_client, ActivityEntry(area="login", kind="login_failed", actor="user", address="192.0.2.7",
                                    details={"count": 3, "first": "2026-11-09T08:01:00", "last": "2026-11-09T08:09:00"}))
    html = ready_client.get(PAGE).text
    assert "Protokoll" in html
    row = text_of(table_rows(html)[0])
    assert "Anmeldung fehlgeschlagen" in row and "3 Versuche" in row and "Benutzer" in row


def test_an_order_taken_back_shows_amount_supplier_and_what_arrived(ready_client):
    """Spec 10.7 (p280): "Bestellung zurückgenommen" with ordered amount, supplier and what arrived."""
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    with sm_of(ready_client).begin() as s:  # after the line of the language change
        s.query(ActivityEntry).delete()
    add(
        ready_client,
        ActivityEntry(area="master", kind="reorder_withdrawn", actor="user", at=datetime(2026, 11, 9, 8, 0),
                      details={"type": "part", "id": 1, "name": "Insert M4", "packs": 4, "size": 50, "unit": "pcs",
                               "supplier": "Example Supplier", "supplier_position": 0, "arrived": 100}),
        ActivityEntry(area="master", kind="reorder_withdrawn", actor="user", at=datetime(2026, 11, 9, 9, 0),
                      details={"type": "filament", "id": 2, "name": "Brand A PLA Black", "packs": 2, "size": 500,
                               "unit": "g", "supplier": None, "supplier_position": 1, "arrived": 0}),
    )
    rows = [text_of(row) for row in table_rows(ready_client.get(PAGE).text)]
    assert "Bestellung zurückgenommen: Brand A PLA Black" in rows[0]
    assert "Bestellt: 2 × 500 g" in rows[0] and "Lieferant: Lieferant 2" in rows[0]
    assert "eingetroffen" not in rows[0]  # nothing arrived: no line for it
    assert "Bestellung zurückgenommen: Insert M4" in rows[1]
    assert "Bestellt: 4 × 50 Stk." in rows[1] and "Lieferant: Example Supplier" in rows[1]
    assert "Davon eingetroffen: 100 Stk." in rows[1]


def test_a_set_rebuilt_by_the_update_names_what_was_dropped(ready_client):
    """Spec 5.4 (p340): the update names the choice articles a set could not keep, from "System"."""
    with sm_of(ready_client).begin() as s:
        s.query(ActivityEntry).delete()
    add(ready_client, ActivityEntry(area="master", kind="set_migrated", actor="system", at=datetime(2026, 11, 11, 8, 0),
                                    details={"id": 4, "name": "Bracket kit", "dropped": ["Handle", "Strip"]}))
    html = ready_client.get(PAGE).text
    row = text_of(table_rows(html)[0])
    assert "Set rebuilt by the update: Bracket kit" in row
    assert "Dropped: Handle, Strip" in row
    assert 'href="/sets/4/edit"' in table_rows(html)[0]
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    rows = [text_of(row) for row in table_rows(ready_client.get(PAGE).text)]
    assert any("Set beim Update umgebaut: Bracket kit" in row and "Entfallen: Handle, Strip" in row for row in rows)
