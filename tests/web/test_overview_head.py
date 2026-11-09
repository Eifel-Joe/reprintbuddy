"""The head line of the overview (Spec 10.2 "Kopfzeile"): the state of the connection to Bambuddy,
the time of the last query and the main switch of the automation (Spec 7.7).

The main switch is the same setting as the one of Settings -> Automation, written by one function
(`set_main_automation`): here as a switch of its own with both tokens, which sends the wanted state.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from markupsafe import escape

from conftest import csrf_of, form_token_of, sm_of
from reprintbuddy import settings as st
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate

ACTION = "/settings/automation/main"


def en(key, **params):
    return str(escape(translate("en", key, **params)))


def region(page, name):
    found = re.search(rf"<!--live:{name}-->(.*?)<!--/live:{name}-->", page, re.S)
    assert found, f"no region {name}"
    return found.group(1)


def status_of(client):
    return client.app.state.ticker.status


def main_switch(client) -> bool:
    with sm_of(client)() as s:
        return bool(st.get_setting(s, "automation"))


def switch_post(client, enabled, *, page="/", **extra):
    data = {
        "csrf_token": csrf_of(client, page),
        "form_token": form_token_of(client, page, ACTION),
        "enabled": enabled,
        "next": page,
        **extra,
    }
    return client.post(ACTION, data=data)


# ---- the head line -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "state", "tone"),
    [
        ({"configured": False}, "not_configured", "neutral"),
        ({"configured": True, "reachable": None}, "unknown", "neutral"),
        ({"configured": True, "reachable": True}, "connected", "ok"),
        ({"configured": True, "reachable": False}, "unreachable", "bad"),
        ({"configured": True, "reachable": True, "poll_error": "403 Forbidden"}, "rejected", "bad"),
    ],
)
def test_connection_badge(ready_client, fields, state, tone):
    for name, value in fields.items():
        setattr(status_of(ready_client), name, value)
    head = region(ready_client.get("/").text, "status")
    assert en("overview.bambuddy") in head
    assert f'<span class="badge tone-{tone}">{en("overview.connection." + state)}</span>' in head


def test_last_query_in_the_head_not_twice(ready_client):
    page = ready_client.get("/").text
    head = region(page, "status")
    assert en("overview.last_poll") in head and en("overview.never_polled") in head
    status_of(ready_client).last_poll_at = datetime(2026, 11, 9, 13, 5, tzinfo=timezone.utc)
    page = ready_client.get("/").text
    head = region(page, "status")
    assert en("overview.never_polled") not in head
    from reprintbuddy.web.formatting import format_local

    assert f'<span class="status-value">{format_local(status_of(ready_client).last_poll_at, "en")}</span>' in head
    # The warnings below do not repeat the line on the overview (the queue page keeps it).
    assert "last-poll" not in region(page, "warnings")
    assert "last-poll" in region(ready_client.get("/queue").text, "warnings")


def test_head_region_follows_bambuddy_and_the_settings(ready_client):
    page = ready_client.get("/").text
    assert 'data-live-region="status" data-live-topics="bambuddy settings"' in page
    only = ready_client.get("/", headers={regions.HEADER: "status"})
    assert only.status_code == 200
    assert en("overview.bambuddy") in only.text


def test_switch_shows_the_stored_state(ready_client):
    head = region(ready_client.get("/").text, "status")
    form = re.search(rf'<form[^>]*action="{ACTION}"[^>]*>.*?</form>', head, re.S).group(0)
    assert 'aria-checked="false"' in form
    assert 'name="enabled" value="1"' in form  # it asks for the other state
    assert 'name="form_token"' in form and 'name="csrf_token"' in form
    assert en("overview.automation_off") in head
    with sm_of(ready_client)() as s:
        st.set_setting(s, "automation", True)
    head = region(ready_client.get("/").text, "status")
    assert 'aria-checked="true"' in head
    assert en("overview.automation_on") in head


def test_german_head(ready_client):
    with sm_of(ready_client)() as s:
        st.set_setting(s, "language", "de")
    head = region(ready_client.get("/").text, "status")
    assert "Letzte Abfrage" in head
    assert "Automatik" in head


# ---- the main switch -----------------------------------------------------------------------


def test_switch_on_and_off(ready_client):
    done = switch_post(ready_client, "1")
    assert done.status_code == 303 and done.headers["location"] == "/"
    assert main_switch(ready_client) is True
    assert en("overview.automation_now_on") in ready_client.get("/").text
    assert switch_post(ready_client, "0").status_code == 303
    assert main_switch(ready_client) is False
    assert en("overview.automation_now_off") in ready_client.get("/").text


def test_wanted_state_is_not_a_toggle(ready_client):
    switch_post(ready_client, "1")
    switch_post(ready_client, "1")
    assert main_switch(ready_client) is True


def test_bad_value_changes_nothing(ready_client):
    done = switch_post(ready_client, "maybe")
    assert done.status_code == 303 and done.headers["location"] == "/"
    assert main_switch(ready_client) is False
    assert en("overview.automation_bad_value") in ready_client.get("/").text


def test_back_to_another_local_page(ready_client):
    done = switch_post(ready_client, "1", next="/queue")
    assert done.headers["location"] == "/queue"


def test_next_must_be_local(ready_client):
    done = switch_post(ready_client, "1", next="https://elsewhere.test/")
    assert done.headers["location"] == "/"


def test_needs_csrf(ready_client):
    token = form_token_of(ready_client, "/", ACTION)
    assert ready_client.post(ACTION, data={"form_token": token, "enabled": "1"}).status_code == 403
    assert main_switch(ready_client) is False


def test_form_token_missing_or_forged_changes_nothing(ready_client):
    for token in (None, "forged"):
        data = {"csrf_token": csrf_of(ready_client, "/"), "enabled": "1", "next": "/"}
        if token:
            data["form_token"] = token
        done = ready_client.post(ACTION, data=data)
        assert done.status_code == 303 and done.headers["location"] == "/"
        assert main_switch(ready_client) is False
        assert en("form.expired") in ready_client.get("/").text


def test_form_token_works_once(ready_client):
    data = {
        "csrf_token": csrf_of(ready_client, "/"),
        "form_token": form_token_of(ready_client, "/", ACTION),
        "enabled": "1",
        "next": "/",
    }
    ready_client.post(ACTION, data=data)
    again = ready_client.post(ACTION, data={**data, "enabled": "0"})
    assert again.status_code == 303
    assert main_switch(ready_client) is True
    assert en("form.duplicate") in ready_client.get("/").text


def test_settings_card_follows_the_switch(ready_client):
    """One setting, two places: the card of the settings shows what the overview switched."""
    switch_post(ready_client, "1")
    page = ready_client.get("/settings?tab=automation").text
    field = re.search(r'<input[^>]*name="automation"[^>]*>', page).group(0)
    assert " checked" in field


def test_blocked_while_an_update_runs(ops_env):
    from reprintbuddy.ops.status import UpdateStatus, write_status
    from test_ops_update import data_dir

    token = csrf_of(ops_env.client, "/")
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=started))
    response = ops_env.client.post(ACTION, data={"csrf_token": token, "enabled": "1"})
    assert response.status_code == 503
    assert main_switch(ops_env.client) is False
