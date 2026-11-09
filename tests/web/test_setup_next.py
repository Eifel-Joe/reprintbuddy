"""A page opened before the Bambuddy step is done (a scanned QR code) is not lost (p114): after
the setup the person lands where they were going. Only local paths are ever followed."""

import pytest
from conftest import csrf_of, finish_bambuddy_setup, landed, mock_auth_me
from starlette.requests import Request

from reprintbuddy.web import deps

URL = "http://bambuddy.test:8000"
KEY = "bb_SETUPKEY_123456"
CONNECT = "/setup/bambuddy/connect"
PAGE = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
BACKSLASH, CR, LF, TAB, NUL = chr(92), chr(13), chr(10), chr(9), chr(0)


def connect(client, respx_mock, **me):
    mock_auth_me(respx_mock, **me)
    token = csrf_of(client, CONNECT)
    # p179: a good check ends on the page that shows the result; "Continue" leads where the redirect led before.
    return landed(client.post(CONNECT, data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY}))


def walk_setup(client):
    for path in ("/setup/bambuddy", "/setup/bambuddy/user", "/setup/bambuddy/key", CONNECT):
        client.get(path, headers=PAGE, follow_redirects=True)


def page_request(path, *, session=None, method="GET", query=b"", accept="text/html"):
    """A request built by hand, so the path reaches the app exactly as written (a test client
    normalises addresses before sending them)."""
    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": query,
        "scheme": "http", "server": ("testserver", 80), "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept", accept.encode())],
        "session": {} if session is None else session,
    }
    return Request(scope)


# ---- the way through the setup ----


def test_a_page_opened_before_the_setup_is_where_the_setup_ends(admin_client, respx_mock):
    first = admin_client.get("/a/5", headers=PAGE)
    assert first.status_code == 303 and first.headers["location"] == "/setup/bambuddy"
    walk_setup(admin_client)
    done = connect(admin_client, respx_mock)
    assert done.status_code == 200
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
    # The key is saved now; the setup page of a finished installation leads to the settings.
    assert admin_client.get(CONNECT).headers["location"] == "/settings?tab=bambuddy"


def test_an_administrator_key_still_ends_on_the_settings(admin_client, respx_mock):
    admin_client.get("/a/5", headers=PAGE)
    done = connect(admin_client, respx_mock, is_admin=True)
    assert done.headers["location"] == "/settings?tab=bambuddy"


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


# ---- what is never kept ----


@pytest.mark.parametrize("path", ["/setup/bambuddy/key", "/logout", "/static/css/reprintbuddy.css", "/healthz"])
def test_pages_of_the_setup_and_the_like_are_not_kept(admin_client, respx_mock, path):
    admin_client.get(path, headers=PAGE)
    assert connect(admin_client, respx_mock).headers["location"] == "/"


def test_a_request_that_is_not_a_page_is_not_kept(admin_client, respx_mock):
    admin_client.get("/a/5", headers={"Accept": "application/json"})
    admin_client.get("/a/6")  # no preference for HTML (*/*): a script, not a person
    assert connect(admin_client, respx_mock).headers["location"] == "/"


def test_a_post_is_not_kept(admin_client, respx_mock):
    admin_client.post("/a/5", headers=PAGE)
    assert connect(admin_client, respx_mock).headers["location"] == "/"


def test_a_remembered_page_is_dropped_once_the_setup_is_done_elsewhere(admin_client):
    admin_client.get("/a/5", headers=PAGE)
    finish_bambuddy_setup(admin_client)  # done in another browser: the key is stored
    admin_client.app.state.bambuddy_factory = lambda: None
    assert admin_client.get("/settings", headers=PAGE).status_code == 200  # a page of the ready app
    assert admin_client.get(CONNECT).headers["location"] == "/settings?tab=bambuddy"


def test_odd_paths_that_do_reach_the_app_end_on_a_local_page(admin_client, respx_mock):
    for raw in ("/a/1%0d%0aX-Injected:%201", "/%2f%2fevil.example", "/%5Cevil.example"):
        admin_client.get(raw, headers=PAGE)
    location = connect(admin_client, respx_mock).headers["location"]
    assert location.startswith("/") and not location.startswith("//")
    assert "://" not in location and CR not in location and LF not in location and BACKSLASH not in location


# ---- the two functions on their own, with paths exactly as written ----


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/x",
        f"/{BACKSLASH}evil.example",
        f"/{TAB}/evil.example",
        f"/a/1{NUL}",
        f"/a/1{CR}{LF}X: y",
        "/",
        "",
        "evil",
        "http://evil.example",
    ],
)
def test_nothing_foreign_or_odd_is_remembered(path):
    """Either refused, or (Starlette rebuilds the path: a backslash is encoded, a line break dropped,
    a missing slash added) a plain path on this very site: one slash, nothing that could break a header."""
    request = page_request(path)
    deps.remember_page(request)
    kept = request.session.get(deps.SETUP_NEXT_KEY)
    assert kept is None or (
        kept.startswith("/")
        and not kept.startswith("//")
        and kept.isprintable()
        and BACKSLASH not in kept
        and kept != "/"
    )
    for refused in ("//evil.example/x", f"/{TAB}/evil.example", f"/a/1{NUL}", "/", ""):
        if path == refused:
            assert kept is None


@pytest.mark.parametrize("path", ["/a/5", "/inspection", "/articles/3/edit"])
def test_a_plain_local_page_is_remembered_as_it_is(path):
    query = "article=5" if path == "/inspection" else ""
    request = page_request(path, query=query.encode())
    deps.remember_page(request)
    assert request.session[deps.SETUP_NEXT_KEY] == path + (f"?{query}" if query else "")


@pytest.mark.parametrize("method,accept", [("POST", "text/html"), ("GET", "application/json"), ("GET", "*/*"), ("GET", "")])
def test_only_a_get_that_asks_for_html_is_remembered(method, accept):
    request = page_request("/a/5", method=method, accept=accept)
    deps.remember_page(request)
    assert deps.SETUP_NEXT_KEY not in request.session


@pytest.mark.parametrize(
    "value",
    [
        "//evil.example",
        "http://evil.example",
        "javascript:alert(1)",
        f"/{BACKSLASH}evil",
        f"/a/1{CR}{LF}X: y",
        f"/a{NUL}",
        "",
        None,
        5,
        "/",
    ],
)
def test_a_tampered_value_in_the_session_is_never_followed(value):
    request = page_request("/setup/bambuddy/connect", session={deps.SETUP_NEXT_KEY: value})
    assert deps.take_remembered_page(request) == "/"
    assert deps.SETUP_NEXT_KEY not in request.session  # used up, also when it was refused


def test_a_good_value_is_followed_once():
    request = page_request("/x", session={deps.SETUP_NEXT_KEY: "/a/5"})
    assert deps.take_remembered_page(request) == "/a/5"
    assert deps.take_remembered_page(request) == "/"


# ---- the same before the first account exists (the sister path of the gate) ----


def create_account(client):
    from conftest import finish_admin_setup

    return finish_admin_setup(client)


def test_a_scan_before_the_first_account_still_ends_on_its_page(client, respx_mock):
    first = client.get("/a/5", headers=PAGE)
    assert first.status_code == 303 and first.headers["location"] == "/setup"
    create_account(client)  # the account is made: the session is renewed, the page is kept
    walk_setup(client)
    assert connect(client, respx_mock).headers["location"] == "/a/5"


def test_the_last_page_before_the_first_account_counts(client, respx_mock):
    client.get("/a/5", headers=PAGE)
    client.get("/inspection?article=5", headers=PAGE)
    create_account(client)
    assert connect(client, respx_mock).headers["location"] == "/inspection?article=5"


@pytest.mark.parametrize("path", ["/login", "/logout", "/setup/bambuddy", "/setup/bambuddy/key", "/static/x.css"])
def test_pages_of_the_setup_and_sign_in_are_not_kept_before_the_first_account_either(client, respx_mock, path):
    client.get(path, headers=PAGE)
    create_account(client)
    assert connect(client, respx_mock).headers["location"] == "/"


def test_nothing_is_kept_before_the_first_account_for_a_script_or_a_post(client, respx_mock):
    client.get("/a/5", headers={"Accept": "application/json"})
    client.post("/a/6", headers=PAGE)
    create_account(client)
    assert connect(client, respx_mock).headers["location"] == "/"


def test_a_failed_account_form_keeps_the_page_for_the_next_try(client, respx_mock):
    client.get("/a/5", headers=PAGE)
    token = csrf_of(client, "/setup")
    bad = client.post("/setup", data={"csrf_token": token, "username": "", "password": "x", "password_repeat": "y", "language": "en"})
    assert bad.status_code == 200
    create_account(client)
    assert connect(client, respx_mock).headers["location"] == "/a/5"


def test_a_page_kept_before_the_account_is_checked_again_where_it_is_used(client):
    """The cookie is signed, but the value is checked on the way out as well: the function that reads it
    refuses anything that is not a plain local path (see the tests above for odd values)."""
    request = page_request("/setup", session={deps.SETUP_NEXT_KEY: "//evil.example"})
    assert deps.take_remembered_page(request) == "/"
