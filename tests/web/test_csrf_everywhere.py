"""Every route that writes needs a signed-in session and a CSRF token.

The routes are discovered from the application, so a route added by a later
building block is covered without touching this file. Path parameters are
filled with `1` so that routes such as `/articles/{id}/book` are checked too.
"""

import re

import pytest
from conftest import ADMIN, PASSWORD, api_routes, csrf_of, make_client
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from reprintbuddy.web.csrf import csrf_token, verify_csrf

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Routes that only exist in an early state of the installation, and a page that
# carries a token in that state. Every other route is tested when fully set up.
EARLY = {
    "/setup": ("fresh", "/setup"),
    "/setup/bambuddy/connect": ("admin", "/setup/bambuddy/connect"),
}


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "1", path)


def write_routes(app) -> list[tuple[str, str]]:
    return sorted(
        {
            (method, concrete(route.path))
            for route in api_routes(app)
            for method in route.methods
            if method in WRITE_METHODS
        }
    )


def states(tmp_path, ready_client):
    fresh = make_client(tmp_path / "fresh.db")
    admin = make_client(tmp_path / "admin.db")
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
    return {"fresh": fresh, "admin": admin, "ready": ready_client}


def where(path):
    return EARLY.get(path, ("ready", "/settings"))


def test_the_discovery_finds_the_known_write_routes(ready_client):
    found = {path for _, path in write_routes(ready_client.app)}
    assert {"/login", "/logout", "/setup", "/setup/bambuddy/connect", "/settings/language"} <= found


def test_routes_with_path_parameters_are_included():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="x")

    @app.get("/token")
    async def token(request):  # pragma: no cover - only used to start a session
        return {"t": csrf_token(request)}

    @app.post("/articles/{article_id}/book", dependencies=[Depends(verify_csrf)])
    async def book(article_id: int):
        return {"booked": article_id}

    @app.delete("/files/{name:path}", dependencies=[Depends(verify_csrf)])
    async def remove(name: str):
        return {"removed": name}

    assert ("POST", "/articles/1/book") in write_routes(app)
    assert ("DELETE", "/files/1") in write_routes(app)
    client = TestClient(app)
    assert client.post("/articles/1/book").status_code == 403


def test_every_write_route_refuses_a_missing_or_forged_token(tmp_path, ready_client):
    clients = states(tmp_path, ready_client)
    primed = {}
    for method, path in write_routes(ready_client.app):
        state, prime = where(path)
        client = clients[state]
        primed.setdefault((state, prime), csrf_of(client, prime))  # a session with a token exists
        for label, kwargs in (
            ("missing", {"data": {}}),
            ("forged", {"data": {"csrf_token": "forged"}}),
            ("forged header", {"data": {}, "headers": {"X-CSRF-Token": "forged"}}),
        ):
            response = client.request(method, path, **kwargs)
            assert response.status_code == 403, f"{method} {path} accepted a {label} token ({response.status_code})"


def test_every_write_route_accepts_a_valid_token(tmp_path, ready_client):
    """The positive control: the refusals above are about the token, not about something else."""
    clients = states(tmp_path, ready_client)
    routes = write_routes(ready_client.app)
    # signing out ends the session, so it comes last
    ordered = [r for r in routes if r[1] != "/logout"] + [r for r in routes if r[1] == "/logout"]
    for method, path in ordered:
        state, prime = where(path)
        client = clients[state]
        token = csrf_of(client, prime)
        response = client.request(method, path, data={"csrf_token": token})
        assert response.status_code != 403, f"{method} {path} refused a valid token"


def test_every_write_route_refuses_a_signed_out_visitor(ready_client):
    ready_client.cookies.clear()
    for method, path in write_routes(ready_client.app):
        response = ready_client.request(method, path, data={})
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


# --- tokens with characters outside ASCII --------------------------------------------------------


@pytest.mark.parametrize("bad", ["ü", "café", "​", "\U0001f600", "a" * 5000])
def test_a_token_with_unusual_characters_is_a_403_not_a_server_error(ready_client, bad):
    csrf_of(ready_client, "/settings")
    response = ready_client.post("/logout", data={"csrf_token": bad})
    assert response.status_code == 403


def test_a_header_token_with_unusual_characters_is_a_403(ready_client):
    csrf_of(ready_client, "/settings")
    for value in ("é".encode("latin-1"), b"\xff\xfe", b"\x80abc"):
        response = ready_client.post("/logout", data={}, headers={"X-CSRF-Token": value})
        assert response.status_code == 403, value


def test_the_right_token_still_works_after_that(ready_client):
    token = csrf_of(ready_client, "/settings")
    ready_client.post("/logout", data={"csrf_token": "ü"})
    assert ready_client.post("/logout", data={"csrf_token": token}).status_code == 303


@pytest.mark.parametrize("raw", [b"csrf_token=%ED%A0%80", b"csrf_token=%FF%FE", b"csrf_token=%C3%28"])
def test_a_percent_encoded_token_that_is_not_valid_utf8_is_a_403(ready_client, raw):
    csrf_of(ready_client, "/settings")
    response = ready_client.post(
        "/logout", content=raw, headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 403
