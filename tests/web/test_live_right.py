"""The right for Bambuddy's live channel, `websocket:connect`, in the set-up and the check (Spec 11.3 a-c, 7.6, 14; p170).

Like the right to upload it is recommended, not required: without it the check still saves, no notice
opens after an update, and Reprintbuddy only asks at the set interval."""

import re

from conftest import REQUIRED, csrf_of, mock_auth_me

from reprintbuddy.bambuddy import connection
from reprintbuddy.web.i18n import translate

LIVE = "websocket:connect"
CONNECT = "/setup/bambuddy/connect"
URL, KEY = "http://bambuddy.test:8000", "bb_SETUPKEY_123456"


def en(key, **params):
    return translate("en", key, **params)


def _optional(html):
    found = re.findall(r'<li class="perm optional (ok|bad)"[^>]*>.*?<code>([^<]+)</code>', html, re.S)
    return {code: state for state, code in found}


def _post(client, respx_mock, permissions):
    mock_auth_me(respx_mock, permissions=permissions)
    token = csrf_of(client, CONNECT)
    return client.post(CONNECT, data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY})


def test_the_right_is_recommended_not_required():
    assert LIVE in connection.OPTIONAL_PERMISSIONS and LIVE not in connection.REQUIRED_PERMISSIONS
    assert connection.EDITOR_LABELS[LIVE] == "Connect Websocket"  # how Bambuddy's group editor names it


def test_the_check_reads_the_right_from_the_granted_ones():
    body = {"username": "reprintbuddy", "permissions": [*REQUIRED, LIVE], "groups": []}
    assert connection._evaluate(body).can_live is True
    body["permissions"] = list(REQUIRED)
    check = connection._evaluate(body)
    assert check.ok and check.can_live is False and check.missing == ()  # nothing is refused for it


def test_the_group_guide_lists_it_among_the_recommended_rights(admin_client):
    html = admin_client.get("/setup/bambuddy/user").text
    optional = html[html.index(en("bambuddy.user.optional")) :]
    optional = optional[: optional.index(en("bambuddy.user.key_rights"))]
    assert "Connect Websocket (<code>websocket:connect</code>)" in optional
    assert en("permission.websocket:connect") in optional
    required = html[: html.index(en("bambuddy.user.optional"))]
    assert LIVE not in required


def test_a_failed_check_shows_it_apart_with_a_tick_or_a_cross(admin_client, respx_mock):
    without = [p for p in REQUIRED if p != "queue:create"]
    assert _optional(_post(admin_client, respx_mock, without).text)[LIVE] == "bad"
    assert _optional(_post(admin_client, respx_mock, [*without, LIVE]).text)[LIVE] == "ok"


def test_a_good_check_without_it_saves_and_says_what_is_missing(admin_client, respx_mock):
    response = _post(admin_client, respx_mock, list(REQUIRED))
    assert response.status_code == 200
    assert en("bambuddy.live_right_missing") in response.text
    assert en("bambuddy.live_right_ok") not in response.text
    assert admin_client.get("/").status_code == 200  # saved, the interface is open


def test_a_good_check_with_it_says_the_live_channel_is_allowed(admin_client, respx_mock):
    response = _post(admin_client, respx_mock, [*REQUIRED, LIVE])
    assert en("bambuddy.live_right_ok") in response.text
    assert en("bambuddy.live_right_missing") not in response.text


def test_german_texts_exist_and_differ():
    for key in ("bambuddy.live_right_ok", "bambuddy.live_right_missing", "permission.websocket:connect"):
        assert translate("de", key) != translate("en", key) != key


def test_the_settings_check_says_it_too(ready_client, respx_mock):
    def check(permissions):
        mock_auth_me(respx_mock, permissions=permissions)
        token = csrf_of(ready_client, "/settings")
        response = ready_client.post(
            "/settings/bambuddy", data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY}
        )
        assert response.status_code == 303
        return ready_client.get(response.headers["location"]).text

    assert en("bambuddy.live_right_missing") in check(list(REQUIRED))
    assert en("bambuddy.live_right_ok") in check([*REQUIRED, LIVE])


def test_no_notice_after_an_update_when_only_the_live_right_is_missing(ready_client, respx_mock):
    mock_auth_me(respx_mock, permissions=list(REQUIRED))
    rights = ready_client.app.state.rights
    rights.status = "unknown"
    token = csrf_of(ready_client, "/settings")
    ready_client.post("/bambuddy/rights/recheck", data={"csrf_token": token, "next": "/"})
    assert rights.status == "ok" and rights.notice is None
