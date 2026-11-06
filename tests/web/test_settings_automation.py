"""Settings, section "Automation" (Spec 7.3, 7.6, 7.7, 10.3; plan B4 Task 11)."""

import re

import pytest

from conftest import csrf_of, sm_of
from reprintbuddy import settings as st
from reprintbuddy.web.i18n import translate

KEYS = ("automation", "queue_running", "queue_cap", "insert_next", "poll_minutes")


def en(key, **params):
    return translate("en", key, **params)


def field(page: str, name: str) -> str:
    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
    assert match, f"no field {name}"
    return match.group(0)


def checked(page: str, name: str) -> bool:
    return " checked" in field(page, name)


def value(page: str, name: str) -> str:
    return re.search(r'value="([^"]*)"', field(page, name)).group(1)


def stored(client) -> dict[str, object]:
    with sm_of(client)() as s:
        return {key: st.get_setting(s, key) for key in KEYS}


def save(client, **fields):
    data = {"csrf_token": csrf_of(client, "/settings")}
    data.update(fields)
    return client.post("/settings/automation", data=data)


def test_defaults_shown(ready_client):
    page = ready_client.get("/settings").text
    assert 'id="automation"' in page
    assert not checked(page, "automation")
    assert not checked(page, "queue_stopped")
    assert value(page, "queue_cap") == "10"
    assert not checked(page, "insert_next")
    assert value(page, "poll_minutes") == "15"


def test_automation_card_is_a_live_region(ready_client):
    page = ready_client.get("/settings").text
    assert "<!--live:automation-->" in page and "<!--/live:automation-->" in page
    assert 'data-live-region="automation" data-live-topics="settings"' in page
    region = page[page.index("<!--live:automation-->") : page.index("<!--/live:automation-->")]
    assert 'action="/settings/automation"' in region and 'id="automation"' in region


def test_save_all(ready_client):
    response = save(
        ready_client, automation="on", queue_stopped="on", queue_cap="3", insert_next="on", poll_minutes="30"
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings#automation"
    assert stored(ready_client) == {
        "automation": True,
        "queue_running": False,
        "queue_cap": 3,
        "insert_next": True,
        "poll_minutes": 30,
    }
    page = ready_client.get("/settings").text
    assert checked(page, "automation") and checked(page, "queue_stopped") and checked(page, "insert_next")
    assert value(page, "queue_cap") == "3" and value(page, "poll_minutes") == "30"


def test_unticked_boxes_switch_off(ready_client):
    save(ready_client, automation="on", queue_stopped="on", queue_cap="3", insert_next="on", poll_minutes="30")
    assert save(ready_client, queue_cap="3", poll_minutes="30").status_code == 303
    assert stored(ready_client) == {
        "automation": False,
        "queue_running": True,
        "queue_cap": 3,
        "insert_next": False,
        "poll_minutes": 30,
    }


@pytest.mark.parametrize(
    "name, bad",
    [("queue_cap", "0"), ("queue_cap", "1000"), ("queue_cap", "abc"), ("poll_minutes", "0"), ("poll_minutes", "9" * 20)],
)
def test_invalid_values_save_nothing(ready_client, name, bad):
    before = stored(ready_client)
    fields = {"automation": "on", "queue_stopped": "on", "queue_cap": "3", "insert_next": "on", "poll_minutes": "30"}
    fields[name] = bad
    response = save(ready_client, **fields)
    assert response.status_code == 200
    assert en(f"settings.invalid_{name}") in response.text
    section = response.text[response.text.index('id="automation"') :]
    assert en(f"settings.invalid_{name}") in section[: section.index("</section>")]
    assert value(response.text, name) == bad  # what was typed stays
    assert stored(ready_client) == before


def test_automation_requires_csrf(ready_client):
    before = stored(ready_client)
    assert ready_client.post("/settings/automation", data={"automation": "on"}).status_code == 403
    assert stored(ready_client) == before


def test_automation_german(ready_client):
    with sm_of(ready_client)() as s:
        st.set_setting(s, "language", "de")
    page = ready_client.get("/settings").text
    for text in (
        "Automatik (Hauptschalter)",
        "Warteschlange anhalten",
        "Obergrenze",
        "Als nächsten Druck einfügen",
        "Abfrage-Abstand",
    ):
        assert text in page, text
