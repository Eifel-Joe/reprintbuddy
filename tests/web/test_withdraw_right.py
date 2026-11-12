"""The right to withdraw prints from Bambuddy after a cancel, `queue:delete_own` (Spec 7.2a, 11.3 a-c; p382).

Like the rights to upload and for the live channel it is recommended, not required: without it the
check still saves, no notice opens after an update, and a cancel only shrinks Reprintbuddy's own queue."""

import re

from conftest import REQUIRED, csrf_of, landed, mock_auth_me

from reprintbuddy.bambuddy import connection
from reprintbuddy.web.i18n import translate

WITHDRAW = "queue:delete_own"
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
    return landed(client, client.post(CONNECT, data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY}))


def test_the_right_is_recommended_not_required():
    assert connection.WITHDRAW_PERMISSION == WITHDRAW
    assert WITHDRAW in connection.OPTIONAL_PERMISSIONS and WITHDRAW not in connection.REQUIRED_PERMISSIONS
    assert connection.EDITOR_LABELS[WITHDRAW] == "Delete Own Queue"  # Bambuddy groups.py `_permission_label`
    assert connection.KEY_RIGHT_FOR[WITHDRAW] == "queue"  # auth.py: QUEUE_DELETE_OWN -> can_queue


def test_the_check_reads_the_right_from_the_granted_ones():
    body = {"username": "reprintbuddy", "permissions": [*REQUIRED, WITHDRAW], "groups": []}
    assert connection._evaluate(body).can_withdraw is True
    body["permissions"] = [*REQUIRED, "queue:delete_all"]  # an administrator's key may delete all
    assert connection._evaluate(body).can_withdraw is True
    body["permissions"] = list(REQUIRED)
    check = connection._evaluate(body)
    assert check.ok and check.can_withdraw is False and check.missing == ()


def test_the_group_guide_lists_it_among_the_recommended_rights(admin_client):
    html = admin_client.get("/setup/bambuddy/user").text
    optional = html[html.index(en("bambuddy.user.optional")) :]
    optional = optional[: optional.index(en("bambuddy.user.key_rights"))]
    assert "Delete Own Queue (<code>queue:delete_own</code>)" in optional
    assert en("permission.queue:delete_own") in optional
    required = html[: html.index(en("bambuddy.user.optional"))]
    assert WITHDRAW not in required


def test_a_failed_check_shows_it_apart_with_a_tick_or_a_cross(admin_client, respx_mock):
    without = [p for p in REQUIRED if p != "queue:create"]
    assert _optional(_post(admin_client, respx_mock, without).text)[WITHDRAW] == "bad"
    assert _optional(_post(admin_client, respx_mock, [*without, WITHDRAW]).text)[WITHDRAW] == "ok"


def test_a_good_check_without_it_saves_and_says_what_is_missing(admin_client, respx_mock):
    response = _post(admin_client, respx_mock, list(REQUIRED))
    assert response.status_code == 200
    assert en("bambuddy.withdraw_right_missing") in response.text
    assert en("bambuddy.withdraw_right_ok") not in response.text
    assert admin_client.get("/").status_code == 200  # saved, the interface is open


def test_a_good_check_with_it_says_prints_can_be_withdrawn(admin_client, respx_mock):
    response = _post(admin_client, respx_mock, [*REQUIRED, WITHDRAW])
    assert en("bambuddy.withdraw_right_ok") in response.text
    assert en("bambuddy.withdraw_right_missing") not in response.text


ALL = "queue:delete_all"  # Bambuddy permissions.py: deletes every entry, own ones included


def test_p410_a_group_with_only_delete_all_may_withdraw_after_a_good_check(admin_client, respx_mock):
    """p410: the page after the redirect knew only what was kept in the session, and only "Delete Own
    Queue" was kept; Bambuddy lets either right delete (print_queue.py `require_ownership_permission`)."""
    response = _post(admin_client, respx_mock, [*REQUIRED, ALL])
    assert en("bambuddy.withdraw_right_ok") in response.text
    assert en("bambuddy.withdraw_right_missing") not in response.text


def test_p410_a_failed_check_ticks_the_right_for_delete_all_too(admin_client, respx_mock):
    """Sister path: the list of recommended rights after a check that lacks a required right."""
    without = [p for p in REQUIRED if p != "queue:create"]
    assert _optional(_post(admin_client, respx_mock, [*without, ALL]).text)[WITHDRAW] == "ok"


def test_the_settings_check_says_it_too(ready_client, respx_mock):
    def check(permissions):
        mock_auth_me(respx_mock, permissions=permissions)
        token = csrf_of(ready_client, "/settings")
        response = ready_client.post(
            "/settings/bambuddy", data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY}
        )
        assert response.status_code == 303
        return ready_client.get(response.headers["location"]).text

    assert en("bambuddy.withdraw_right_missing") in check(list(REQUIRED))
    assert en("bambuddy.withdraw_right_ok") in check([*REQUIRED, WITHDRAW])
    assert en("bambuddy.withdraw_right_ok") in check([*REQUIRED, ALL])  # p410


def test_no_notice_after_an_update_when_only_this_right_is_missing(ready_client, respx_mock):
    mock_auth_me(respx_mock, permissions=list(REQUIRED))
    rights = ready_client.app.state.rights
    rights.status = "unknown"
    token = csrf_of(ready_client, "/settings")
    ready_client.post("/bambuddy/rights/recheck", data={"csrf_token": token, "next": "/"})
    assert rights.status == "ok" and rights.notice is None


def test_german_texts_exist_and_differ():
    for key in ("bambuddy.withdraw_right_ok", "bambuddy.withdraw_right_missing", "permission.queue:delete_own"):
        assert translate("de", key) != translate("en", key) != key
