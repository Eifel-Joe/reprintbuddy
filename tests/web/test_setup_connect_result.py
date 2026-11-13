"""Set-up step 4 after a successful check shows the owner and every required right with a tick
(Spec 11.3 c, p179). Before, the list was only there when the check failed."""

import re

from conftest import REQUIRED, csrf_of, landed, mock_auth_me
from texts import en

from reprintbuddy.web.i18n import translate

CONNECT = "/setup/bambuddy/connect"
URL, KEY = "http://bambuddy.test:8000", "bb_SETUPKEY_123456"
PAGE = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _post(client, respx_mock, **me):
    mock_auth_me(respx_mock, **me)
    token = csrf_of(client, CONNECT)
    return landed(client, client.post(CONNECT, data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY}))


def _rights(html):
    found = re.findall(r'<li class="perm (ok|bad)"[^>]*>.*?<code>([^<]+)</code>', html, re.S)
    return {code: state for state, code in found}


def _continue_target(html):
    found = re.search(r'<a class="btn" href="([^"]+)"[^>]*>\s*' + re.escape(en("bambuddy.connected.continue")), html)
    assert found, "no continue link"
    return found.group(1)


def test_success_shows_owner_and_every_required_right_ticked(admin_client, respx_mock):
    response = _post(admin_client, respx_mock)
    assert response.status_code == 200
    assert en("bambuddy.result.owner", owner="reprintbuddy") in response.text
    rights = _rights(response.text)
    assert all(rights[code] == "ok" for code in REQUIRED)
    assert "bad" not in {state for code, state in rights.items() if code in REQUIRED}
    assert en("bambuddy.result.fix") not in response.text  # the hint for a missing right is for a failure


def test_success_is_saved_and_the_form_is_gone(admin_client, respx_mock):
    response = _post(admin_client, respx_mock)
    assert response.status_code == 200
    assert 'action="/setup/bambuddy/connect"' not in response.text  # nothing left to enter
    assert 'class="perm optional' not in response.text  # the optional right is one sentence, not a list here
    assert KEY not in response.text
    with admin_client.app.state.sessionmaker() as s:
        from reprintbuddy import settings as st

        assert st.get_setting(s, "bambuddy_url") == URL
    assert admin_client.get("/").status_code == 200  # the interface is open


def test_success_says_whether_uploading_works_and_only_there(admin_client, respx_mock):
    response = _post(admin_client, respx_mock)  # REQUIRED has no upload right
    assert en("bambuddy.upload_right_missing") in response.text
    assert en("bambuddy.upload_right_missing") not in admin_client.get("/").text  # no second notice on the next page


def test_success_with_the_upload_right(admin_client, respx_mock):
    response = _post(admin_client, respx_mock, permissions=[*REQUIRED, "library:upload"])
    assert en("bambuddy.upload_right_ok") in response.text


def test_continue_leads_to_the_start_page(admin_client, respx_mock):
    assert _continue_target(_post(admin_client, respx_mock).text) == "/"


def test_continue_leads_to_the_page_opened_before_the_setup(admin_client, respx_mock):
    admin_client.get("/a/5", headers=PAGE)
    assert _continue_target(_post(admin_client, respx_mock).text) == "/a/5"


def test_an_administrator_key_is_saved_with_the_warning_and_continues_to_the_settings(admin_client, respx_mock):
    response = _post(admin_client, respx_mock, is_admin=True)
    assert response.status_code == 200
    tag = response.text[response.text.rindex("<p", 0, response.text.index("The key belongs to an administrator")) :]
    assert tag.startswith('<p class="alert" role="alert"')
    assert _continue_target(response.text) == "/settings?tab=bambuddy"


def test_german(admin_client, respx_mock):
    from reprintbuddy import settings as st

    with admin_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    response = _post(admin_client, respx_mock)
    assert translate("de", "bambuddy.result.owner", owner="reprintbuddy") in response.text
    assert translate("de", "bambuddy.connected.continue") in response.text


def test_a_second_post_after_the_success_goes_to_the_settings(admin_client, respx_mock):
    _post(admin_client, respx_mock)
    token = csrf_of(admin_client, "/settings")
    again = admin_client.post(CONNECT, data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY})
    assert again.status_code == 303 and again.headers["location"] == "/settings?tab=bambuddy"
