"""The return address after a sign-in or an action stays on this server (p69).

`safe_next` is the one place that decides. It let control characters through; they cannot be
used to leave the server, but a browser strips a tab or a line break from an address before it
reads it, so the check must not be fooled by them (`/<TAB>/evil.example` is `//evil.example` to a
browser). Encoded forms (`%09`, `%5c`, `%2f`, twice encoded) are looked at in the path only: a
filter in the query may carry any encoded value.
"""

import pytest
from conftest import login as _login

from reprintbuddy.web.deps import local_next, safe_next

BS = chr(92)  # one backslash, spelled out so no quoting layer can eat it

# Every one of these must end on the start page.
FOREIGN = [
    # the old cases
    "//evil.example",
    "https://evil.example",
    "http://evil.example/",
    "/" + BS + "evil.example",
    BS + BS + "evil.example",
    "evil.example",
    "javascript:alert(1)",
    "",
    None,
    # a browser drops tab, CR and LF inside an address
    "/\t/evil.example",
    "/\n/evil.example",
    "/\r/evil.example",
    "/\r\n/evil.example",
    "/\t\t/evil.example",
    "/\x00/evil.example",
    "/\x0b/evil.example",
    "/\x0c/evil.example",
    "/\x1b/evil.example",
    "/\x7f/evil.example",
    "/\x85/evil.example",  # C1 control
    "/ok\x00",
    "/ok\r\nSet-Cookie: a=b",
    # format and separator characters (invisible)
    "/\u200b/evil.example",
    "/\u200e/evil.example",
    "/\u2028/evil.example",
    "/\u2029/evil.example",
    "/\ufeff/evil.example",
    # backslashes anywhere
    "/a" + BS + "b",
    "/a?x=" + BS + "evil",
    # more slashes
    "///evil.example",
    "////evil.example",
    "/.//evil.example",
    "/a/..//evil.example",
    # encoded in the path
    "/%09/evil.example",
    "/%0a/evil.example",
    "/%0d%0a/evil.example",
    "/%00",
    "/%5cevil.example",
    "/%5Cevil.example",
    "/%2f/evil.example",
    "/%2F%2Fevil.example",
    "/a/%2f%2fevil.example",
    "/%e2%80%8b/evil.example",  # zero-width space
    # twice (and three times) encoded in the path
    "/%252f%252fevil.example",
    "/%255cevil.example",
    "/%2509/evil.example",
    "/%25252f%25252fevil.example",
    "/%2525252f/evil",  # encoded four times: still changing after the last round of decoding
    # too long
    "/" + "a" * 3000,
    # raw control characters in the query or the fragment
    "/ok?x=\ny",
    "/ok?x=\t",
    "/ok?x=\x00",
    "/ok#\r",
    "/ok?x=\u200b",
]

LOCAL = [
    "/",
    "/settings",
    "/settings?tab=maintenance",
    "/articles?filter=a%20b&page=2",
    "/articles/12",
    "/articles/12#top",
    "/settings?tab=maintenance#ops-backup",
    # encoded values in the query are the caller's own
    "/articles?q=%2F%2Fevil",
    "/articles?q=%5C",
    "/articles?q=%0A",
    "/articles?q=%252f",
    "/articles?next=https%3A%2F%2Fexample.org%2F",
    # an address inside a query is only text
    "/search?u=http://example.org/a//b",
    # a space and non-ASCII letters are ordinary
    "/articles?q=ä ö",
    "/ärtikel/ü",
    "/a%20b",
    "/a%25b",  # a literal percent sign
]


@pytest.mark.parametrize("value", FOREIGN, ids=lambda v: repr(v)[:50])
def test_foreign_or_odd_targets_go_to_the_start_page(value):
    assert safe_next(value) == "/"


@pytest.mark.parametrize("value", LOCAL, ids=lambda v: repr(v)[:50])
def test_local_targets_stay_as_they_are(value):
    assert safe_next(value) == value


def test_a_path_of_dots_is_kept_when_nothing_hides_in_it():
    assert safe_next("/./") == "/./"


def test_local_next_agrees_with_safe_next():
    class Form(dict):
        pass

    assert local_next(Form(next="/settings?tab=x")) == "/settings?tab=x"
    assert local_next(Form(next="/\t/evil.example")) == ""
    assert local_next(Form(next="/%2f/evil.example")) == ""
    assert local_next(Form()) == ""


def test_a_value_that_is_not_text_is_refused():
    assert safe_next(None) == "/"
    assert safe_next("") == "/"


# --- in the application ------------------------------------------------------------------------------


@pytest.fixture
def logged_out(admin_client):
    admin_client.cookies.clear()
    return admin_client


@pytest.mark.parametrize(
    "target",
    ["/\t/evil.example", "/\r\n/evil.example", "/%09/evil.example", "/%2f/evil.example", "/%252f%252fevil.example"],
)
def test_the_sign_in_does_not_follow_a_disguised_foreign_target(logged_out, target):
    response = _login(logged_out, next=target)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_the_sign_in_follows_a_local_target_with_an_encoded_query(logged_out):
    response = _login(logged_out, next="/settings?tab=maintenance&q=%2F%2Fx")
    assert response.headers["location"] == "/settings?tab=maintenance&q=%2F%2Fx"


def test_the_sign_in_page_does_not_carry_a_disguised_target(logged_out):
    page = logged_out.get("/login", params={"next": "/\t/evil.example"})
    assert page.status_code == 200
    assert "evil.example" not in page.text
    page = logged_out.get("/login", params={"next": "/%09/evil.example"})
    assert "evil.example" not in page.text


def test_the_sidebar_toggle_does_not_follow_a_disguised_foreign_target(ready_client):
    response = ready_client.get("/ui/sidebar", params={"collapsed": "1", "next": "/\t/evil.example"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    response = ready_client.get("/ui/sidebar", params={"collapsed": "1", "next": "/settings?tab=x"})
    assert response.headers["location"] == "/settings?tab=x"


def test_the_gate_sends_a_stranger_to_the_sign_in_with_the_page_asked_for(logged_out):
    response = logged_out.get("/settings", params={"tab": "maintenance"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fsettings%3Ftab%3Dmaintenance"
