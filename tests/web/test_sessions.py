"""Sessions live in a table: log out ends only this session, a password change ends all others."""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ADMIN, PASSWORD, csrf_of
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import func, select

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.cli import main
from reprintbuddy.models import LoginSession

COOKIE = "reprintbuddy_session"


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cookie_payload(client) -> dict:
    payload = client.cookies.get(COOKIE).split(".")[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode())


def sid_of(client) -> str:
    return cookie_payload(client)["sid"]


def copy_of(client) -> TestClient:
    """Another browser holding a copy of this one's session cookie."""
    other = TestClient(client.app, follow_redirects=False)
    other.cookies.set(COOKIE, client.cookies.get(COOKIE))
    return other


def independent_login(app) -> TestClient:
    """A second browser that signs in on its own: its own session."""
    other = TestClient(app, follow_redirects=False)
    token = csrf_of(other, "/login")
    response = other.post("/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD})
    assert response.status_code == 303
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


def logout(client):
    token = csrf_of(client, "/settings")
    return client.post("/logout", data={"csrf_token": token})


def rows(client) -> list[LoginSession]:
    with client.app.state.sessionmaker() as s:
        return list(s.scalars(select(LoginSession)))


def test_a_copy_of_the_cookie_works_until_the_session_ends(ready_client):
    assert is_logged_in(copy_of(ready_client))


def test_the_cookie_carries_a_session_id_and_the_table_stores_only_its_hash(ready_client):
    sid = sid_of(ready_client)
    ids = [row.id for row in rows(ready_client)]
    assert sid not in ids
    assert auth.session_id_hash(sid) in ids


def test_session_rows_have_creation_and_last_seen_times(ready_client):
    (row,) = rows(ready_client)
    assert abs((now() - row.created_at).total_seconds()) < 60
    assert abs((now() - row.last_seen_at).total_seconds()) < 60


def test_password_change_ends_all_other_sessions_but_not_the_own(ready_client):
    copy = copy_of(ready_client)
    other_device = independent_login(ready_client.app)
    assert is_logged_in(copy) and is_logged_in(other_device)
    assert change_password(ready_client).status_code == 303
    assert not is_logged_in(copy)
    assert not is_logged_in(other_device)
    assert is_logged_in(ready_client)  # issued anew
    assert len(rows(ready_client)) == 1


def test_logout_ends_only_this_session(ready_client):
    other_device = independent_login(ready_client.app)
    copy = copy_of(ready_client)
    assert logout(ready_client).status_code == 303
    assert not is_logged_in(ready_client)
    assert not is_logged_in(copy)  # a copied cookie is as dead as the original
    assert is_logged_in(other_device)  # the other device stays signed in
    assert len(rows(ready_client)) == 1


def test_set_password_command_ends_all_sessions(ready_client, db_path, monkeypatch):
    other_device = independent_login(ready_client.app)
    answers = iter(["another-pass-9", "another-pass-9"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["set-password", "--username", "admin", "--db", str(db_path)]) == 0
    assert not is_logged_in(ready_client)
    assert not is_logged_in(other_device)
    assert rows(ready_client) == []


def forge(client, payload: dict) -> TestClient:
    with client.app.state.sessionmaker() as s:
        secret = st.ensure_session_secret(s)
    raw = base64.b64encode(json.dumps(payload).encode())
    forged = TestClient(client.app, follow_redirects=False)
    forged.cookies.set(COOKIE, TimestampSigner(secret).sign(raw).decode())
    return forged


@pytest.mark.parametrize(
    "payload",
    [{"uid": 1}, {"uid": 1, "sid": "not-a-known-session"}, {"sid": "x"}, {"uid": 1, "sid": 5}],
    ids=["no-sid", "unknown-sid", "no-uid", "sid-not-text"],
)
def test_a_cookie_that_names_no_live_session_is_rejected(ready_client, payload):
    assert not is_logged_in(forge(ready_client, payload))


def test_a_cookie_for_another_user_id_is_rejected(ready_client):
    forged = forge(ready_client, {"uid": 999, "sid": sid_of(ready_client)})
    assert not is_logged_in(forged)


def test_last_seen_is_touched_at_most_once_a_minute(ready_client):
    digest = auth.session_id_hash(sid_of(ready_client))
    stale = now() - timedelta(minutes=5)
    with ready_client.app.state.sessionmaker() as s:
        s.get(LoginSession, digest).last_seen_at = stale
        s.commit()
    assert is_logged_in(ready_client)
    (row,) = rows(ready_client)
    assert row.last_seen_at > stale + timedelta(minutes=4)  # refreshed
    refreshed = row.last_seen_at
    assert is_logged_in(ready_client)
    (row,) = rows(ready_client)
    assert row.last_seen_at == refreshed  # not written again within the minute


def test_an_idle_session_expires_and_is_removed(ready_client):
    digest = auth.session_id_hash(sid_of(ready_client))
    with ready_client.app.state.sessionmaker() as s:
        s.get(LoginSession, digest).last_seen_at = now() - auth.SESSION_MAX_IDLE - timedelta(days=1)
        s.commit()
    assert not is_logged_in(ready_client)
    assert rows(ready_client) == []


def test_old_sessions_are_pruned_when_a_new_one_starts(ready_client):
    with ready_client.app.state.sessionmaker() as s:
        for n in range(3):
            old = now() - auth.SESSION_MAX_IDLE - timedelta(days=2)
            s.add(LoginSession(id=f"old-{n}", created_at=old, last_seen_at=old))
        s.commit()
    independent_login(ready_client.app)
    ids = {row.id for row in rows(ready_client)}
    assert not any(i.startswith("old-") for i in ids)
    assert len(ids) == 2  # the first browser and the new one


def test_session_helpers_directly(ready_client):
    with ready_client.app.state.sessionmaker() as s:
        sid = auth.create_session(s)
        assert auth.session_valid(s, sid) is True
        assert auth.session_valid(s, "nope") is False
        auth.end_session(s, sid)
        assert auth.session_valid(s, sid) is False
        auth.end_session(s, sid)  # ending twice is harmless
        auth.end_session(s, None)
        before = s.scalar(select(func.count()).select_from(LoginSession))
        auth.end_all_sessions(s)
        assert s.scalar(select(func.count()).select_from(LoginSession)) == 0
        assert before >= 1


def test_last_seen_is_not_touched_while_writing_rests(ready_client):
    """p271: during an update a row written now could be dropped by a rollback; the login stays valid."""
    digest = auth.session_id_hash(sid_of(ready_client))
    stale = now() - timedelta(minutes=5)
    with ready_client.app.state.sessionmaker() as s:
        s.get(LoginSession, digest).last_seen_at = stale
        s.commit()
    ready_client.app.state.update_starting = True
    try:
        assert is_logged_in(ready_client)
    finally:
        ready_client.app.state.update_starting = False
    (row,) = rows(ready_client)
    assert row.last_seen_at == stale
    assert is_logged_in(ready_client)
    (row,) = rows(ready_client)
    assert row.last_seen_at > stale  # refreshed again once writing is allowed


def test_an_idle_session_is_refused_but_not_removed_while_writing_rests(ready_client):
    """Sister path of p271: removing the expired row is a write too."""
    digest = auth.session_id_hash(sid_of(ready_client))
    with ready_client.app.state.sessionmaker() as s:
        s.get(LoginSession, digest).last_seen_at = now() - auth.SESSION_MAX_IDLE - timedelta(days=1)
        s.commit()
    ready_client.app.state.update_starting = True
    try:
        assert not is_logged_in(ready_client)
    finally:
        ready_client.app.state.update_starting = False
    assert len(rows(ready_client)) == 1
    assert not is_logged_in(ready_client)
    assert rows(ready_client) == []
