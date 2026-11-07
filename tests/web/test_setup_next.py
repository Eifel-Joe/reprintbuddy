"""A page opened before the Bambuddy step is done (a scanned QR code) is not lost (p114): after
the setup the person lands where they were going. Only local paths are ever followed."""

import pytest
from conftest import csrf_of, mock_auth_me

URL = "http://bambuddy.test:8000"
KEY = "bb_SETUPKEY_123456"
CONNECT = "/setup/bambuddy/connect"
PAGE = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def connect(client, respx_mock, **me):
    mock_auth_me(respx_mock, **me)
    token = csrf_of(client, CONNECT)
    return client.post(CONNECT, data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY})


def walk_setup(client):
    for path in ("/setup/bambuddy", "/setup/bambuddy/user", "/setup/bambuddy/key", CONNECT):
        client.get(path, headers=PAGE, follow_redirects=True)


def test_a_page_opened_before_the_setup_is_where_the_setup_ends(admin_client, respx_mock):
    first = admin_client.get("/a/5", headers=PAGE)
    assert first.status_code == 303 and first.headers["location"] == "/setup/bambuddy"
    walk_setup(admin_client)
    done = connect(admin_client, respx_mock)
    assert done.status_code == 303
    assert done.headers["location"] == "/a/5"


def test_the_query_of_the_page_is_kept(admin_client, respx_mock):
    admin_client.get("/inspection?article=5", headers=PAGE)
    assert connect(admin_client, respx_mock).headers["location"] == "/inspection?article=5"


def test_without_a_page_before_it_the_setup_ends_on_the_start_page_as_before(admin_client, respx_mock):
    assert connect(admin_client, respx_mock).headers["location"] == "/"


def test_the_start_page_is_no_page_to_return_to_but_still_ends_on_it(admin_client, respx_mock):
    admin_client.get("/", headers=PAGE)
    assert connect(admin_client, respx_mock).headers["location"] == "/"


def test_the_last_page_opened_counts(admin_client, respx_mock):
    admin_client.get("/a/5", headers=PAGE)
    admin_client.get("/a/6", headers=PAGE)
    assert connect(admin_client, respx_mock).headers["location"] == "/a/6"


def test_it_is_used_once(admin_client, respx_mock):
    admin_client.get("/a/5", headers=PAGE)
    assert connect(admin_client, respx_mock).headers["location"] == "/a/5"
    # The key is saved now; a second visit of the setup page leads to the settings, never to /a/5.
    assert admin_client.get(CONNECT).headers["location"] == "/settings?tab=bambuddy"


def test_an_administrator_key_still_ends_on_the_settings_and_drops_the_page(admin_client, respx_mock):
    admin_client.get("/a/5", headers=PAGE)
    done = connect(admin_client, respx_mock, is_admin=True)
    assert done.headers["location"] == "/settings?tab=bambuddy"


# ---- what is never kept ----


@pytest.mark.parametrize(
    "path",
    [
        "/setup/bambuddy/key",  # the setup itself
        "/logout",
        "/static/css/reprintbuddy.css",
        "/healthz",
    ],
)
def test_pages_of_the_setup_and_the_like_are_not_kept(admin_client, respx_mock, path):
    admin_client.get(path, headers=PAGE)
    assert connect(admin_client, respx_mock).headers["location"] == "/"


def test_a_request_that_is_not_a_page_is_not_kept(admin_client, respx_mock):
    admin_client.get("/a/5", headers={"Accept": "application/json"})
    admin_client.get("/a/6")  # no Accept header preference at all (*/*): a script, not a person
    assert connect(admin_client, respx_mock).headers["location"] == "/"


def test_a_post_is_not_kept(admin_client, respx_mock):
    admin_client.post("/a/5", headers=PAGE)
    assert connect(admin_client, respx_mock).headers["location"] == "/"


@pytest.mark.parametrize(
    "raw",
    [
        "//evil.example/x",
        "/\\evil.example",
        "/a/1%0d%0aX-Injected:%201",
        "/a/1%0aSet-Cookie:%20x=1",
        "/%2e%2e/x%00",
    ],
)
def test_nothing_foreign_or_odd_is_followed(admin_client, respx_mock, raw):
    admin_client.get(raw, headers=PAGE)
    location = connect(admin_client, respx_mock).headers["location"]
    assert "://" not in location and "\\" not in location  # never another site
    assert "\r" not in location and "\n" not in location and "\x00" not in location
    assert location.startswith("/") and not location.startswith("//")


def test_a_tampered_cookie_cannot_make_the_setup_leave_the_site(admin_client, respx_mock):
    """The target lives in the signed session; even if a value like this got in, the redirect
    after the setup re-checks it."""
    from reprintbuddy.web import deps

    assert deps.safe_next("//evil.example") == "/"
    assert deps.safe_next("http://evil.example") == "/"
    assert deps.safe_next("javascript:alert(1)") == "/"


def test_the_setup_is_not_left_by_a_second_account_or_a_ready_client(ready_client):
    """Once set up, the gate keeps nothing and `/setup/bambuddy/connect` goes to the settings."""
    ready_client.get("/a/5", headers=PAGE)
    assert ready_client.get(CONNECT).headers["location"] == "/settings?tab=bambuddy"
    assert ready_client.get(CONNECT).headers["location"] == "/settings?tab=bambuddy"


def test_after_the_login_the_scanned_page_is_still_asked_for_and_then_the_setup_follows(client, respx_mock):
    """The whole way of a scan on a phone: not signed in -> login -> (setup not done) -> setup -> page."""
    from conftest import ADMIN, PASSWORD, finish_admin_setup

    finish_admin_setup(client)
    client.cookies.clear()
    scan = client.get("/a/5", headers=PAGE)
    assert scan.headers["location"].startswith("/login?next=")
    token = csrf_of(client, "/login")
    login = client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD, "next": "/a/5"})
    assert login.headers["location"] == "/a/5"
    gate = client.get("/a/5", headers=PAGE)
    assert gate.headers["location"] == "/setup/bambuddy"
    assert connect(client, respx_mock).headers["location"] == "/a/5"
