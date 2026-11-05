"""Secrets must not appear in any page, error message or the session cookie."""

import base64
import json

from conftest import PASSWORD, api_routes, csrf_of

API_KEY = "bb_LEAKCHECK_123456"
TLS_KEY = "-----BEGIN PRIVATE KEY-----LEAKCHECK"
NEW_PASSWORD = "LEAKCHECK-password-9"
CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
SECRETS = ("LEAKCHECK", NEW_PASSWORD, PASSWORD)


def _store_secrets(client, respx_mock):
    respx_mock.get("http://bambuddy.test:8000/api/v1/queue/").respond(200, json=[])
    token = csrf_of(client, "/settings")
    assert (
        client.post(
            "/settings/bambuddy",
            data={
                "csrf_token": token,
                "bambuddy_url": "http://bambuddy.test:8000",
                "bambuddy_username": "lagerbuddy",
                "bambuddy_api_key": API_KEY,
                "bambuddy_review_confirmed": "on",
            },
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/settings/server",
            data={
                "csrf_token": token,
                "host": "0.0.0.0",
                "port": "8443",
                "tls_cert": CERT,
                "tls_key": TLS_KEY + "\n-----END PRIVATE KEY-----\n",
            },
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/settings/password",
            data={
                "csrf_token": token,
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
                "new_password_repeat": NEW_PASSWORD,
            },
        ).status_code
        == 303
    )


def _assert_clean(response, where):
    body = response.text
    for secret in SECRETS:
        assert secret not in body, f"{secret!r} leaked in {where}"


def test_no_get_page_leaks_a_secret(ready_client, respx_mock):
    _store_secrets(ready_client, respx_mock)
    paths = sorted(
        {
            route.path
            for route in api_routes(ready_client.app)
            if "GET" in route.methods
            and "{" not in route.path
        }
    )
    assert "/settings" in paths and "/" in paths  # the walk really covers the pages
    for path in paths:
        _assert_clean(ready_client.get(path), f"GET {path}")
    settings_page = ready_client.get("/settings").text
    assert "********" in settings_page


def test_pages_logged_out_leak_nothing(ready_client, respx_mock):
    _store_secrets(ready_client, respx_mock)
    ready_client.cookies.clear()
    for path in ("/", "/settings", "/login", "/setup", "/setup/bambuddy", "/healthz"):
        _assert_clean(ready_client.get(path), f"logged-out GET {path}")


def test_error_pages_do_not_echo_secrets(ready_client, respx_mock):
    token = csrf_of(ready_client, "/settings")
    respx_mock.get("http://other.test:8000/api/v1/queue/").respond(401)
    wrong_key = ready_client.post(
        "/settings/bambuddy",
        data={
            "csrf_token": token,
            "bambuddy_url": "http://other.test:8000",
            "bambuddy_username": "lagerbuddy",
            "bambuddy_api_key": API_KEY,
            "bambuddy_review_confirmed": "on",
        },
    )
    assert wrong_key.status_code == 200
    _assert_clean(wrong_key, "rejected Bambuddy key")

    bad_pem = ready_client.post(
        "/settings/server",
        data={"csrf_token": token, "host": "0.0.0.0", "port": "8443", "tls_cert": CERT, "tls_key": "LEAKCHECK"},
    )
    assert bad_pem.status_code == 200
    _assert_clean(bad_pem, "rejected TLS key")

    bad_password = ready_client.post(
        "/settings/password",
        data={
            "csrf_token": token,
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "new_password_repeat": "different-LEAKCHECK",
        },
    )
    assert bad_password.status_code == 200
    _assert_clean(bad_password, "rejected password change")


def test_session_cookie_holds_no_secret(ready_client, respx_mock):
    _store_secrets(ready_client, respx_mock)
    cookie = ready_client.cookies.get("lagerbuddy_session")
    payload = cookie.split(".")[0]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    assert set(json.loads(decoded)) <= {"uid", "csrf", "flash"}
    for secret in SECRETS:
        assert secret not in decoded
