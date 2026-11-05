"""Sessions can be revoked: password change, log out and `set-password` end them."""

import base64
import json

import pytest
from conftest import PASSWORD, csrf_of
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from reprintbuddy import settings as st
from reprintbuddy.cli import main

COOKIE = "reprintbuddy_session"


def second_browser(client) -> TestClient:
    """Another browser holding a copy of this one's session cookie."""
    other = TestClient(client.app, follow_redirects=False)
    other.cookies.set(COOKIE, client.cookies.get(COOKIE))
    return other


def is_logged_in(client) -> bool:
    return client.get("/").status_code == 200


def change_password(client, new="brand-new-pass1"):
    token = csrf_of(client, "/settings")
    return client.post(
        "/settings/password",
        data={
            "csrf_token": token,
            "current_password": PASSWORD,
            "new_password": new,
            "new_password_repeat": new,
        },
    )


def test_copy_of_the_cookie_works_until_something_revokes_it(ready_client):
    other = second_browser(ready_client)
    assert is_logged_in(other)


def test_password_change_ends_other_sessions_but_not_the_own(ready_client):
    other = second_browser(ready_client)
    assert is_logged_in(other)
    assert change_password(ready_client).status_code == 303
    assert not is_logged_in(other)
    assert other.get("/").headers["location"].startswith("/login")
    assert is_logged_in(ready_client)  # the own session was issued anew
    assert ready_client.get("/settings").status_code == 200


def test_logout_makes_a_copied_cookie_useless(ready_client):
    other = second_browser(ready_client)
    token = csrf_of(ready_client, "/settings")
    assert ready_client.post("/logout", data={"csrf_token": token}).status_code == 303
    assert not is_logged_in(other)
    assert not is_logged_in(ready_client)


def test_set_password_command_ends_sessions(ready_client, db_path, monkeypatch):
    other = second_browser(ready_client)
    answers = iter(["another-pass-9", "another-pass-9"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["set-password", "--username", "admin", "--db", str(db_path)]) == 0
    assert not is_logged_in(other)
    assert not is_logged_in(ready_client)


def _forge(client, payload: dict) -> str:
    with client.app.state.sessionmaker() as s:
        secret = st.ensure_session_secret(s)
    raw = base64.b64encode(json.dumps(payload).encode())
    return TimestampSigner(secret).sign(raw).decode()


@pytest.mark.parametrize(
    "payload",
    [{"uid": 1}, {"uid": 1, "tok": "0" * 32}, {"tok": "x"}],
    ids=["no-token", "wrong-token", "no-uid"],
)
def test_cookie_without_valid_token_is_rejected(ready_client, payload):
    forged = TestClient(ready_client.app, follow_redirects=False)
    forged.cookies.set(COOKIE, _forge(ready_client, payload))
    assert not is_logged_in(forged)


def test_session_generation_helpers(client):
    with client.app.state.sessionmaker() as s:
        assert st.session_generation(s) == 0
        st.bump_session_generation(s)
        st.bump_session_generation(s)
        assert st.session_generation(s) == 2
        with pytest.raises(KeyError):
            st.set_setting(s, "session_generation", 5)


def test_session_token_depends_on_secret_hash_and_generation():
    from reprintbuddy.auth import session_token

    base = session_token("secret", "hash", 0)
    assert base == session_token("secret", "hash", 0)
    assert len(base) >= 32
    assert base != session_token("other", "hash", 0)
    assert base != session_token("secret", "other-hash", 0)
    assert base != session_token("secret", "hash", 1)
