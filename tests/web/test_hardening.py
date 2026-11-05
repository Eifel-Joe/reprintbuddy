"""Security headers, the Host check during setup, readable error pages, `next` with query."""

import pytest
from conftest import csrf_of

from reprintbuddy import settings as st

HEADERS = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


def assert_hardened(response):
    for name, value in HEADERS.items():
        assert response.headers.get(name) == value, (name, response.headers.get(name))


# --- security headers --------------------------------------------------------


def test_headers_on_health_and_static(ready_client):
    assert_hardened(ready_client.get("/healthz"))
    assert_hardened(ready_client.get("/static/css/reprintbuddy.css"))


def test_headers_on_pages_and_redirects(client, ready_client):
    assert_hardened(ready_client.get("/"))
    assert_hardened(ready_client.get("/settings"))
    ready_client.cookies.clear()
    redirect = ready_client.get("/settings")
    assert redirect.status_code == 303
    assert_hardened(redirect)
    assert_hardened(ready_client.get("/login"))


def test_headers_on_error_responses(ready_client):
    assert_hardened(ready_client.get("/no-such-page"))
    assert_hardened(ready_client.post("/logout", data={}))


def test_headers_before_setup(client):
    assert_hardened(client.get("/setup"))
    assert_hardened(client.get("/"))


# --- Host header during setup (DNS rebinding) --------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "testserver",
        "localhost:8080",
        "127.0.0.1:8080",
        "192.0.2.10:8080",
        "[::1]:8080",
        "reprintbuddy",
        "reprintbuddy:8080",
        "box.local",
        "box.lan:8080",
    ],
)
def test_setup_accepts_local_looking_hosts(client, host):
    assert client.get("/setup", headers={"host": host}).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com",
        "evil.example.com:8080",
        "localhost.evil.example.com",
        "box.local.evil.example",
        "reprintbuddy.fritz.box",  # .box is a public top-level domain: anybody can register one
        "anything.box",
    ],
)
def test_setup_refuses_other_host_names(client, host):
    response = client.get("/setup", headers={"host": host})
    assert response.status_code == 400
    assert "IP address" in response.text
    assert_hardened(response)


def test_setup_post_with_foreign_host_creates_nothing(client):
    token = csrf_of(client, "/setup")
    response = client.post(
        "/setup",
        headers={"host": "evil.example.com"},
        data={
            "csrf_token": token,
            "username": "attacker",
            "password": "password123",
            "password_repeat": "password123",
            "language": "en",
        },
    )
    assert response.status_code == 400
    with client.app.state.sessionmaker() as s:
        from reprintbuddy import auth

        assert not auth.has_user(s)


def test_any_host_is_fine_once_the_account_exists(admin_client):
    admin_client.cookies.clear()
    assert admin_client.get("/login", headers={"host": "reprintbuddy.example.org"}).status_code == 200


def test_health_check_ignores_the_host(client):
    assert client.get("/healthz", headers={"host": "evil.example.com"}).status_code == 200


# --- error pages -------------------------------------------------------------


def test_csrf_failure_shows_a_readable_page(ready_client):
    response = ready_client.post("/logout", data={})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "Not allowed" in response.text
    assert "sidebar-nav" in response.text  # inside the layout, signed in
    assert '"detail"' not in response.text


def test_unauthenticated_post_shows_the_bare_page(admin_client):
    admin_client.cookies.clear()
    response = admin_client.post("/settings/language", data={})
    assert response.status_code == 403
    assert "Not allowed" in response.text
    assert "sidebar-nav" not in response.text


def test_unknown_page_is_a_readable_404(ready_client):
    response = ready_client.get("/no-such-page")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Page not found" in response.text
    assert "sidebar-nav" in response.text


def test_closed_setup_is_a_readable_404(admin_client):
    response = admin_client.get("/setup")
    assert response.status_code == 404
    assert "Page not found" in response.text


def test_error_pages_follow_the_language(ready_client):
    with ready_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    assert "Seite nicht gefunden" in ready_client.get("/no-such-page").text
    assert "Nicht erlaubt" in ready_client.post("/logout", data={}).text


def test_error_page_links_back(ready_client):
    assert 'href="/"' in ready_client.get("/no-such-page").text


def test_wrong_method_is_a_readable_error(ready_client):
    response = ready_client.get("/logout")
    assert response.status_code == 405
    assert response.headers["content-type"].startswith("text/html")


# --- next keeps the query string ----------------------------------------------


def test_login_redirect_keeps_the_query_string(admin_client):
    admin_client.cookies.clear()
    response = admin_client.get("/settings?tab=server&x=1")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fsettings%3Ftab%3Dserver%26x%3D1"


def test_login_returns_to_the_page_with_its_query(admin_client):
    admin_client.cookies.clear()
    location = admin_client.get("/settings?tab=server&x=1").headers["location"]
    page = admin_client.get(location)
    token = csrf_of(admin_client, location)
    assert page.status_code == 200
    response = admin_client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "admin",
            "password": "password123",
            "next": "/settings?tab=server&x=1",
        },
    )
    assert response.headers["location"] == "/settings?tab=server&x=1"
