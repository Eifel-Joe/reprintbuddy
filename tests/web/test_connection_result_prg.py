"""The result of a good check is shown once, on a page of its own, after a redirect (p191, p192; Spec 11.3 c).

Setup step 4 and Settings -> Bambuddy use the same block: key owner, every required right with a tick, the
sentences about uploading and the live channel, the note for an administrator's key. Saving redirects
(Post/Redirect/Get); a reload of the result page must not lose the page a scanned QR code was going to, and
nothing secret goes into the session or an address."""

import base64
import json
import re

from conftest import REQUIRED, csrf_of, mock_auth_me
from texts import en

from reprintbuddy.bambuddy.connection import ConnectionCheck
from reprintbuddy.web import deps
from reprintbuddy.web.i18n import translate

CONNECT, DONE = "/setup/bambuddy/connect", "/setup/bambuddy/done"
SETTINGS = "/settings?tab=bambuddy"
URL, KEY = "http://bambuddy.test:8000", "bb_PRGKEY_123456"
PAGE = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _rights(html):
    found = re.findall(r'<li class="perm (ok|bad)"[^>]*>.*?<code>([^<]+)</code>', html, re.S)
    return {code: state for state, code in found}


def _continue_target(html):
    found = re.search(r'<a class="btn" href="([^"]+)"[^>]*>\s*' + re.escape(en("bambuddy.connected.continue")), html)
    assert found, "no continue link"
    return found.group(1)


def _session(client):
    payload = client.cookies.get("reprintbuddy_session").split(".")[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode())


def _setup_post(client, respx_mock, **me):
    mock_auth_me(respx_mock, **me)
    token = csrf_of(client, CONNECT)
    return client.post(CONNECT, data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY})


def _settings_post(client, respx_mock, **me):
    mock_auth_me(respx_mock, **me)
    token = csrf_of(client, "/settings")
    return client.post(
        "/settings/bambuddy", data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY}
    )


# ---- setup step 4 ----


def test_setup_saves_and_redirects_to_the_result_page(admin_client, respx_mock):
    response = _setup_post(admin_client, respx_mock)
    assert response.status_code == 303 and response.headers["location"] == DONE  # p192: no page as the answer to a POST
    assert KEY not in response.headers["location"]
    with admin_client.app.state.sessionmaker() as s:
        from reprintbuddy import settings as st

        assert st.get_setting(s, "bambuddy_url") == URL  # saved before the redirect


def test_the_result_page_shows_everything_once(admin_client, respx_mock):
    _setup_post(admin_client, respx_mock, permissions=[*REQUIRED, "library:upload"])
    page = admin_client.get(DONE)
    assert page.status_code == 200
    assert en("bambuddy.result.owner", owner="reprintbuddy") in page.text
    rights = _rights(page.text)
    assert rights and all(rights[code] == "ok" for code in REQUIRED)
    assert en("bambuddy.upload_right_ok") in page.text
    assert en("bambuddy.live_right_missing") in page.text
    assert _continue_target(page.text) == "/"
    assert KEY not in page.text


def test_a_reload_of_the_result_page_leaves_for_the_settings(admin_client, respx_mock):
    _setup_post(admin_client, respx_mock)
    admin_client.get(DONE)
    again = admin_client.get(DONE)
    assert again.status_code == 303 and again.headers["location"] == SETTINGS


def test_a_reload_keeps_the_page_a_scanned_code_was_going_to(admin_client, respx_mock):
    admin_client.get("/a/5", headers=PAGE)  # the scan, before the setup is done
    _setup_post(admin_client, respx_mock)
    first = admin_client.get(DONE, headers=PAGE)
    assert _continue_target(first.text) == "/a/5"
    reload_ = admin_client.get(DONE, headers=PAGE)  # F5
    assert reload_.status_code == 303 and reload_.headers["location"] == "/a/5"  # not the settings: the target is not lost


def test_the_remembered_page_is_used_up_by_going_on(admin_client, respx_mock):
    admin_client.get("/a/5", headers=PAGE)
    _setup_post(admin_client, respx_mock)
    admin_client.get(DONE, headers=PAGE)
    admin_client.get("/", headers=PAGE)  # any other page of the interface
    assert deps.SETUP_NEXT_KEY not in _session(admin_client)
    assert admin_client.get(DONE).headers["location"] == SETTINGS


def test_an_administrator_key_continues_to_the_settings_with_the_note(admin_client, respx_mock):
    _setup_post(admin_client, respx_mock, is_admin=True)
    page = admin_client.get(DONE)
    assert en("bambuddy.admin_note") in page.text.replace("&#34;", '"')
    assert _continue_target(page.text) == SETTINGS


def test_the_result_page_is_closed_without_a_check_and_before_the_key_is_saved(admin_client):
    assert admin_client.get(DONE).headers["location"] == "/setup/bambuddy"  # nothing saved: the setup goes on


def test_the_session_holds_only_what_the_page_needs(admin_client, respx_mock):
    _setup_post(admin_client, respx_mock, permissions=[*REQUIRED, "library:upload", "archives:delete_all"])
    session = _session(admin_client)
    entry = session[deps.CONNECTION_RESULT_KEY]
    assert set(entry) == {"owner", "admin", "granted"}
    assert "archives:delete_all" not in entry["granted"]  # a right the page does not name stays out
    assert KEY not in json.dumps(session)


def test_a_failed_check_still_answers_with_the_page(admin_client, respx_mock):
    without = [p for p in REQUIRED if p != "queue:create"]
    response = _setup_post(admin_client, respx_mock, permissions=without)
    assert response.status_code == 200  # nothing saved: the person types again, no redirect
    assert _rights(response.text)["queue:create"] == "bad"
    assert deps.CONNECTION_RESULT_KEY not in _session(admin_client)


def test_german(admin_client, respx_mock):
    from reprintbuddy import settings as st

    with admin_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    _setup_post(admin_client, respx_mock)
    page = admin_client.get(DONE)
    assert translate("de", "bambuddy.result.owner", owner="reprintbuddy") in page.text
    assert translate("de", "bambuddy.live_right_missing") in page.text


# ---- Settings -> Bambuddy ----


def test_settings_saves_and_redirects_to_the_tab(ready_client, respx_mock):
    response = _settings_post(ready_client, respx_mock)
    assert response.status_code == 303 and response.headers["location"] == SETTINGS
    assert KEY not in response.headers["location"]


def test_settings_shows_owner_and_the_list_of_rights_after_a_good_check(ready_client, respx_mock):
    """p191: before, only the owner and the message came back, no list."""
    response = _settings_post(ready_client, respx_mock, permissions=[*REQUIRED, "websocket:connect"])
    page = ready_client.get(response.headers["location"])
    assert en("bambuddy.result.owner", owner="reprintbuddy") in page.text
    rights = _rights(page.text)
    assert rights and all(rights[code] == "ok" for code in REQUIRED)
    assert en("bambuddy.upload_right_missing") in page.text
    assert en("bambuddy.live_right_ok") in page.text
    assert en("settings.saved") in page.text


def test_settings_says_it_once(ready_client, respx_mock):
    response = _settings_post(ready_client, respx_mock)
    first = ready_client.get(response.headers["location"])
    assert en("bambuddy.result.rights") in first.text
    second = ready_client.get(SETTINGS)
    assert en("bambuddy.result.rights") not in second.text  # a reload shows the plain tab
    assert en("bambuddy.upload_right_missing") not in second.text
    assert en("settings.bambuddy_owner", owner="reprintbuddy") in second.text


def test_settings_with_an_administrator_key_shows_the_note_in_the_block(ready_client, respx_mock):
    response = _settings_post(ready_client, respx_mock, is_admin=True)
    page = ready_client.get(response.headers["location"]).text.replace("&#34;", '"')
    assert page.count(en("bambuddy.admin_note")) == 1  # the block says it; no second message with the same text


def test_settings_failed_check_keeps_the_page_with_the_list(ready_client, respx_mock):
    without = [p for p in REQUIRED if p != "queue:create"]
    response = _settings_post(ready_client, respx_mock, permissions=without)
    assert response.status_code == 200
    assert _rights(response.text)["queue:create"] == "bad"
    assert deps.CONNECTION_RESULT_KEY not in _session(ready_client)


def test_another_tab_does_not_use_up_the_result(ready_client, respx_mock):
    _settings_post(ready_client, respx_mock)
    ready_client.get("/settings?tab=general")
    assert en("bambuddy.result.rights") in ready_client.get(SETTINGS).text


# ---- the stored form ----


def test_the_stored_result_gives_back_the_same_check():
    check = ConnectionCheck(
        True, "ok", owner="robot", owner_is_admin=True, granted=frozenset({*REQUIRED, "library:upload", "other:x"})
    )
    session: dict = {}
    deps.store_connection_result(session, check)
    got = deps.take_connection_result(session)
    assert got.ok and got.owner == "robot" and got.owner_is_admin and got.can_upload and not got.can_live
    assert got.missing == () and "other:x" not in got.granted
    assert deps.take_connection_result(session) is None  # once


def test_a_damaged_entry_is_ignored():
    for junk in ("x", 5, {"owner": 5, "admin": "yes", "granted": "no"}, {"owner": "", "admin": False, "granted": []}):
        session = {deps.CONNECTION_RESULT_KEY: junk}
        assert deps.take_connection_result(session) is None
        assert deps.CONNECTION_RESULT_KEY not in session


# ---- review findings ----


def test_a_live_region_fetch_of_another_tab_does_not_use_up_the_result(ready_client, respx_mock):
    """Region mode uses nothing up (Spec 10.5.3): the other tab's fetch must not take what the redirect shows."""
    response = _settings_post(ready_client, respx_mock)
    fetched = ready_client.get(SETTINGS, headers={"X-Live-Regions": "channel"})
    assert fetched.status_code == 200
    page = ready_client.get(response.headers["location"])
    assert en("bambuddy.result.rights") in page.text


def test_a_region_fetch_does_not_use_up_the_remembered_page_either(admin_client, respx_mock):
    admin_client.get("/a/5", headers=PAGE)
    _setup_post(admin_client, respx_mock)
    admin_client.get("/settings?tab=bambuddy", headers={"X-Live-Regions": "channel"})
    assert deps.SETUP_NEXT_KEY in _session(admin_client)
    assert _continue_target(admin_client.get(DONE, headers=PAGE).text) == "/a/5"


def test_a_reload_after_an_administrator_key_goes_where_continue_went(admin_client, respx_mock):
    admin_client.get("/a/5", headers=PAGE)
    _setup_post(admin_client, respx_mock, is_admin=True)
    assert _continue_target(admin_client.get(DONE, headers=PAGE).text) == SETTINGS
    assert admin_client.get(DONE, headers=PAGE).headers["location"] == SETTINGS  # not the page of the scan


def test_a_very_long_owner_name_does_not_fill_the_cookie(admin_client, respx_mock):
    _setup_post(admin_client, respx_mock, username="x" * 5000)
    assert len(admin_client.cookies.get("reprintbuddy_session")) < 3000
    assert admin_client.get(DONE).status_code == 200


def test_settings_german(ready_client, respx_mock):
    from reprintbuddy import settings as st

    with ready_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    response = _settings_post(ready_client, respx_mock)
    page = ready_client.get(response.headers["location"]).text
    assert translate("de", "bambuddy.result.owner", owner="reprintbuddy") in page
    assert translate("de", "bambuddy.upload_right_missing") in page
