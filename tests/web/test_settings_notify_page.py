"""The settings tab "Notifications" with the card "Home Assistant" (Spec 10.6, 10.3, 11.5a).

Home Assistant is never reached: every address ends in `.test` and respx answers.
"""

from __future__ import annotations

import functools
import re
from datetime import datetime

import pytest
from markupsafe import escape
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from sqlalchemy import select

from reprintbuddy import settings as st
from reprintbuddy.models import NotifyMark
from reprintbuddy.web.i18n import translate

PAGE = "/settings?tab=notify"
URL = "http://ha.test:8123"
OTHER = "http://other-ha.test:8123"
CALL = f"{URL}/api/services/notify/mobile_app_phone"
TOKEN = "eyJ0eXAiOiJKV1QifQ.TOKENCHECK.sig"
SERVICE = "notify.mobile_app_phone"


def raw(key, **params):
    return translate("en", key, **params)


def en(key, **params):
    """The English text as the page carries it (escaped: an apostrophe becomes &#39;)."""
    return str(escape(translate("en", key, **params)))


def stored(client, key):
    with sm_of(client)() as s:
        return st.get_setting(s, key)


def form(client, **over):
    data = {
        "csrf_token": csrf_of(client, PAGE),
        "ha_enabled": "on",
        "ha_url": URL,
        "ha_token": TOKEN,
        "ha_service": SERVICE,
        "ha_names": "on",
        "ha_event": list(st.HA_EVENTS),
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def save(client, **over):
    return client.post("/settings/notify", data=form(client, **over))


def send_test(client, **over):
    data = form(client, **over)
    data["form_token"] = form_token_of(client, PAGE, "/settings/notify")
    return client.post("/settings/notify/test", data=data)


# ---- the tab ----------------------------------------------------------------------------------------


def test_the_tab_shows_the_card_with_every_field_and_event(ready_client):
    page = ready_client.get(PAGE).text
    assert 'action="/settings/notify"' in page
    for name in ("ha_enabled", "ha_url", "ha_token", "ha_service", "ha_names"):
        assert f'name="{name}"' in page, name
    for event in st.HA_EVENTS:
        assert f'value="{event}"' in page, event
        assert en(f"notify.event.{event}") in page
    assert 'formaction="/settings/notify/test"' in page
    assert en("notify.push_services_hint") in page  # titles and texts go through Google or Apple
    assert en("notify.ip_ban_hint") in page
    assert en("notify.user_hint") in page
    # off and empty by default; the token field is a password field that password managers leave alone
    assert re.search(r'name="ha_enabled"(?![^>]*checked)', page)
    assert re.search(r'<input id="ha_token" name="ha_token" type="password" value=""', page)


def test_the_tab_sits_before_the_account(ready_client):
    page = ready_client.get("/settings").text
    links = re.findall(r'<a class="tab(?: active)?" href="/settings\?tab=([a-z]+)"', page)
    assert links == ["general", "automation", "labels", "bambuddy", "notify", "account", "maintenance", "log"]


# ---- saving -----------------------------------------------------------------------------------------


def test_saving_stores_everything_and_starts_the_clock(ready_client):
    response = save(ready_client, ha_event=["paused", "trouble"], ha_names=None)
    assert response.status_code == 303 and response.headers["location"] == PAGE
    assert stored(ready_client, "ha_enabled") is True
    assert stored(ready_client, "ha_url") == URL
    assert stored(ready_client, "ha_token") == TOKEN
    assert stored(ready_client, "ha_service") == SERVICE
    assert stored(ready_client, "ha_names") is False
    assert stored(ready_client, "ha_events") == ["paused", "trouble"]
    assert datetime.fromisoformat(stored(ready_client, "ha_since")).tzinfo is not None


def test_the_token_never_shows_again(ready_client):
    save(ready_client)
    page = ready_client.get(PAGE).text
    assert "TOKENCHECK" not in page
    assert st.MASK in page  # the sign that a value is stored
    assert en("settings.secret_stored") in page


def test_an_empty_token_field_keeps_the_token_for_the_same_address(ready_client):
    save(ready_client)
    save(ready_client, ha_token="", ha_service="notify.other_phone")
    assert stored(ready_client, "ha_token") == TOKEN
    assert stored(ready_client, "ha_service") == "notify.other_phone"


def test_a_new_address_needs_the_token_again(ready_client):
    save(ready_client)
    response = save(ready_client, ha_url=OTHER, ha_token="")
    assert response.status_code == 200
    assert en("notify.token_required_new_address") in response.text
    assert stored(ready_client, "ha_url") == URL  # nothing changed
    assert OTHER in response.text  # what was typed stays


def test_switched_off_a_new_address_drops_the_token(ready_client):
    save(ready_client)
    save(ready_client, ha_enabled=None, ha_url=OTHER, ha_token="")
    assert stored(ready_client, "ha_url") == OTHER
    assert stored(ready_client, "ha_token") is None


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("ha_url", "", "notify.fields_required"),
        ("ha_service", "", "notify.fields_required"),
        ("ha_url", "homeassistant:8123", "notify.invalid_url"),
        ("ha_service", "light.turn_on", "notify.invalid_service"),
        ("ha_service", "notify.send_message", "notify.invalid_service"),
        ("ha_token", "with blank", "notify.invalid_token"),
    ],
)
def test_wrong_input_is_refused_and_nothing_is_stored(ready_client, field, value, error):
    response = save(ready_client, **{field: value})
    assert response.status_code == 200
    assert en(error) in response.text
    assert stored(ready_client, "ha_enabled") is False
    assert "TOKENCHECK" not in response.text  # a typed token is never shown back


def test_switched_off_the_fields_may_stay_empty(ready_client):
    response = save(ready_client, ha_enabled=None, ha_url="", ha_token="", ha_service="")
    assert response.status_code == 303
    assert stored(ready_client, "ha_enabled") is False


def test_switching_off_forgets_what_was_reported(ready_client):
    save(ready_client)
    with sm_of(ready_client).begin() as s:
        s.add(NotifyMark(kind="paused", ref="1", state="sent"))
    save(ready_client, ha_enabled=None)
    with sm_of(ready_client)() as s:
        assert s.scalars(select(NotifyMark)).all() == []
    assert stored(ready_client, "ha_since") is None


def test_saving_needs_the_csrf_token(ready_client):
    data = form(ready_client)
    data["csrf_token"] = "wrong"
    assert ready_client.post("/settings/notify", data=data).status_code == 403
    assert stored(ready_client, "ha_enabled") is False


# ---- the test message ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,outcome",
    [(200, "accepted"), (401, "token"), (400, "request"), (403, "blocked"), (502, "server"), (302, "redirect")],
)
def test_the_test_message_uses_what_is_typed_and_shows_a_fixed_text(ready_client, respx_mock, status, outcome):
    headers = {"Location": "http://elsewhere.test/"} if status == 302 else {}
    route = respx_mock.post(CALL).respond(status, headers=headers, text="ANSWERCHECK from Home Assistant")
    response = send_test(ready_client)
    assert response.status_code == 200
    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert en(f"notify.outcome.{outcome}") in response.text
    assert "ANSWERCHECK" not in response.text and "TOKENCHECK" not in response.text
    assert stored(ready_client, "ha_url") is None  # nothing was saved
    assert URL in response.text  # what was typed stays on the page


def test_the_test_message_when_home_assistant_is_not_reachable(ready_client, respx_mock):
    import httpx

    respx_mock.post(CALL).mock(side_effect=httpx.ConnectError("refused"))
    assert en("notify.outcome.unreachable") in send_test(ready_client).text


def test_the_test_message_uses_the_stored_token_only_for_the_same_address(ready_client, respx_mock):
    save(ready_client)
    route = respx_mock.post(CALL).respond(200, json=[])
    assert en("notify.outcome.accepted") in send_test(ready_client, ha_token="").text
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    other = respx_mock.post(f"{OTHER}/api/services/notify/mobile_app_phone").respond(200, json=[])
    response = send_test(ready_client, ha_url=OTHER, ha_token="")
    assert en("notify.token_required_new_address") in response.text
    assert other.call_count == 0  # the stored token never goes to another address


def test_the_test_message_refuses_a_service_outside_notify(ready_client, respx_mock):
    response = send_test(ready_client, ha_service="lock.unlock")
    assert en("notify.invalid_service") in response.text
    assert respx_mock.calls.call_count == 0


def test_the_test_message_text(ready_client, respx_mock):
    import json

    route = respx_mock.post(CALL).respond(200, json=[])
    send_test(ready_client)
    body = json.loads(route.calls.last.request.content)
    assert body["title"] == raw("notify.test.title")
    assert body["message"] == raw("notify.test.text")


def test_a_used_form_token_sends_no_second_test_message(ready_client, respx_mock):
    route = respx_mock.post(CALL).respond(200, json=[])
    data = form(ready_client)
    data["form_token"] = form_token_of(ready_client, PAGE, "/settings/notify")
    ready_client.post("/settings/notify/test", data=data)
    ready_client.post("/settings/notify/test", data=data)
    assert route.call_count == 1


# ---- after a tick: status card and warning ------------------------------------------------------------


def paused_article(client):
    with sm_of(client).begin() as s:
        a = make_article(s, name="Bracket")
        a.paused, a.pause_reason = True, "two_failures"


def test_a_failed_message_shows_the_warning_and_the_last_result(ready_client, respx_mock):
    respx_mock.post(CALL).respond(503)
    save(ready_client)
    paused_article(ready_client)
    with ready_client:  # the app's loop runs the sending task
        ready_client.portal.call(functools.partial(ready_client.app.state.ticker.tick, poll=False))
        ready_client.portal.call(_settle, ready_client)
        overview = ready_client.get("/").text
        assert en("warning.ha_unreachable", reason=raw("notify.outcome.server")) in overview
        page = ready_client.get(PAGE).text
        assert en("notify.outcome.server") in page


async def _settle(client):
    import asyncio

    for _ in range(200):
        task = client.app.state.notifier.task
        if task is None or task.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the sending task did not end")


def test_the_warning_and_status_follow_live(ready_client):
    page = ready_client.get("/").text
    assert re.search(r'data-live-region="warnings" data-live-topics="[^"]*\bnotify\b', page)
    settings = ready_client.get(PAGE).text
    assert re.search(r'data-live-region="notify-status" data-live-topics="[^"]*\bnotify\b', settings)


def test_the_notifier_listens_to_the_engine(ready_client):
    app = ready_client.app
    assert app.state.notifier.after_tick in app.state.ticker.after_tick


def test_after_a_test_with_a_typed_token_the_page_says_to_type_it_again_for_saving(ready_client, respx_mock):
    """The typed token is never written back into the page (Spec 11.5a), so saving needs it again."""
    respx_mock.post(CALL).respond(200, json=[])
    assert en("notify.test.retype_token") in send_test(ready_client).text
    save(ready_client)
    respx_mock.post(CALL).respond(200, json=[])
    assert en("notify.test.retype_token") not in send_test(ready_client, ha_token="").text  # stored one used


def test_a_stored_token_never_goes_to_another_path_of_the_same_host(ready_client, respx_mock):
    """Final review 4: behind a proxy another path can be another service (Spec 10.6: who changes the
    address enters the token again)."""
    save(ready_client, ha_url="http://proxy.test/ha")
    other = respx_mock.post("http://proxy.test/other/api/services/notify/mobile_app_phone").respond(200, json=[])
    response = send_test(ready_client, ha_url="http://proxy.test/other", ha_token="")
    assert en("notify.token_required_new_address") in response.text
    assert other.call_count == 0
    response = save(ready_client, ha_url="http://proxy.test/other", ha_token="")
    assert en("notify.token_required_new_address") in response.text
    assert stored(ready_client, "ha_url") == "http://proxy.test/ha"
    # the same address with a trailing slash is the same address
    same = respx_mock.post("http://proxy.test/ha/api/services/notify/mobile_app_phone").respond(200, json=[])
    send_test(ready_client, ha_url="http://proxy.test/ha/", ha_token="")
    assert same.call_count == 1
