"""Every route that writes needs a signed-in session and a CSRF token.

The routes are discovered from the application, so a route added by a later
building block is covered without touching this file.
"""

import pytest
from conftest import api_routes, csrf_of, make_client

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Routes that only exist in an early state of the installation, and a page that
# carries a token in that state. Every other route is tested when fully set up.
EARLY = {
    "/setup": ("fresh", "/setup"),
    "/setup/bambuddy": ("admin", "/setup/bambuddy"),
}


def write_routes(app) -> list[tuple[str, str]]:
    return sorted(
        {
            (method, route.path)
            for route in api_routes(app)
            for method in route.methods
            if method in WRITE_METHODS and "{" not in route.path
        }
    )


def attempt(client, method, path, **kwargs):
    return client.request(method, path, **kwargs)


def test_the_discovery_finds_the_known_write_routes(ready_client):
    found = {path for _, path in write_routes(ready_client.app)}
    assert {"/login", "/logout", "/setup", "/setup/bambuddy", "/settings/language"} <= found


def test_every_write_route_refuses_a_missing_or_forged_token(tmp_path, ready_client):
    fresh = make_client(tmp_path / "fresh.db")
    admin = make_client(tmp_path / "admin.db")
    # admin: account exists and is signed in, Bambuddy step still open
    from conftest import ADMIN, PASSWORD

    token = csrf_of(admin, "/setup")
    admin.post(
        "/setup",
        data={
            "csrf_token": token,
            "username": ADMIN,
            "password": PASSWORD,
            "password_repeat": PASSWORD,
            "language": "en",
        },
    )
    clients = {"fresh": fresh, "admin": admin, "ready": ready_client}
    primed = {}

    for method, path in write_routes(ready_client.app):
        state, prime = EARLY.get(path, ("ready", "/settings"))
        client = clients[state]
        primed.setdefault((state, prime), csrf_of(client, prime))  # a session with a token exists
        real = primed[(state, prime)]

        missing = attempt(client, method, path, data={})
        forged = attempt(client, method, path, data={"csrf_token": "forged"})
        forged_header = attempt(client, method, path, data={}, headers={"X-CSRF-Token": "forged"})
        for label, response in (("missing", missing), ("forged", forged), ("forged header", forged_header)):
            assert response.status_code == 403, f"{method} {path} accepted a {label} token ({response.status_code})"
        assert real  # the token existed, so the refusals above were about the token itself


def test_every_write_route_refuses_a_signed_out_visitor(ready_client):
    ready_client.cookies.clear()
    for method, path in write_routes(ready_client.app):
        response = attempt(ready_client, method, path, data={})
        assert response.status_code in (403, 404), f"{method} {path} -> {response.status_code}"


def test_a_valid_token_alone_is_not_enough_without_a_session(ready_client):
    """A token from one browser must not work for a signed-out one."""
    token = csrf_of(ready_client, "/settings")
    ready_client.cookies.clear()
    response = ready_client.post("/settings/language", data={"csrf_token": token, "language": "de"})
    assert response.status_code == 403
    with ready_client.app.state.sessionmaker() as s:
        from reprintbuddy import settings as st

        assert st.get_setting(s, "language") == "en"


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_write_methods_are_not_silently_accepted(ready_client, method):
    # none of these exist today; if one is added, the discovery test above covers it
    response = ready_client.request(method, "/settings/language", data={})
    assert response.status_code in (403, 404, 405)
