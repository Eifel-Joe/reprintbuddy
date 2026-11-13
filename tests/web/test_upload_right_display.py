"""The optional right to upload print files in the setup and the settings (Spec 11.3 a–c, 5.1a)."""

import re

from conftest import REQUIRED, csrf_of, landed, mock_auth_me
from texts import en


USER_PAGE, CONNECT = "/setup/bambuddy/user", "/setup/bambuddy/connect"
URL, KEY = "http://bambuddy.test:8000", "bb_testkey"


def _optional(html):
    """{code: 'ok' | 'bad'} of the optional rights in a check result."""
    found = re.findall(r'<li class="perm optional (ok|bad)"[^>]*>.*?<code>([^<]+)</code>', html, re.S)
    return {code: state for state, code in found}


def test_setup_lists_the_upload_right_as_recommended_not_required(admin_client):
    html = admin_client.get(USER_PAGE).text
    assert en("bambuddy.user.optional") in html
    optional = html[html.index(en("bambuddy.user.optional")) :]
    assert "Upload Library (<code>library:upload</code>)" in optional
    assert en("permission.library:upload") in optional
    required = html[: html.index(en("bambuddy.user.optional"))]
    assert "library:upload" not in required


def _connect(client, respx_mock, permissions):
    mock_auth_me(respx_mock, permissions=permissions)
    token = csrf_of(client, CONNECT)
    return landed(client, client.post(CONNECT, data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY}))


def test_a_failed_check_shows_the_upload_right_apart(admin_client, respx_mock):
    without = [p for p in REQUIRED if p != "queue:create"]
    html = _connect(admin_client, respx_mock, without).text
    assert _optional(html) == {"library:upload": "bad", "websocket:connect": "bad", "queue:delete_own": "bad"}
    assert en("bambuddy.result.optional") in html


def test_a_failed_check_with_the_upload_right_ticks_it(admin_client, respx_mock):
    without = [p for p in REQUIRED if p != "queue:create"]
    html = _connect(admin_client, respx_mock, [*without, "library:upload"]).text
    assert _optional(html) == {"library:upload": "ok", "websocket:connect": "bad", "queue:delete_own": "bad"}


def test_setup_saves_without_the_upload_right_and_says_uploading_is_off(admin_client, respx_mock):
    response = _connect(admin_client, respx_mock, list(REQUIRED))
    assert response.status_code == 200  # saved: the right is not required; the result page says it
    assert en("bambuddy.upload_right_missing") in response.text


def test_setup_with_the_upload_right_says_uploading_is_on(admin_client, respx_mock):
    response = _connect(admin_client, respx_mock, [*REQUIRED, "library:upload"])
    assert en("bambuddy.upload_right_ok") in response.text


def _settings_check(client, respx_mock, permissions):
    mock_auth_me(respx_mock, permissions=permissions)
    token = csrf_of(client, "/settings")
    response = client.post(
        "/settings/bambuddy",
        data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY},
    )
    assert response.status_code == 303
    return client.get(response.headers["location"]).text


def test_settings_check_says_whether_uploading_works(ready_client, respx_mock):
    assert en("bambuddy.upload_right_missing") in _settings_check(ready_client, respx_mock, list(REQUIRED))
    assert en("bambuddy.upload_right_ok") in _settings_check(ready_client, respx_mock, [*REQUIRED, "library:upload"])


def test_no_notice_after_an_update_when_only_the_upload_right_is_missing(ready_client, respx_mock):
    """Spec 11.3 "Rights after an update": the optional right never opens the dialog."""
    mock_auth_me(respx_mock, permissions=list(REQUIRED))
    rights = ready_client.app.state.rights
    rights.status = "unknown"
    token = csrf_of(ready_client, "/settings")
    ready_client.post("/bambuddy/rights/recheck", data={"csrf_token": token, "next": "/"})
    assert rights.status == "ok" and rights.notice is None
