"""Sign-in in the activity log (Spec 10.7, group 1): signed in and out with the device's address;
failed sign-ins, also the password asked again before backup, restore or a new password; never the
typed user name or password."""

from __future__ import annotations

from sqlalchemy import select

from conftest import PASSWORD, csrf_of, login as sign_in, sm_of
from reprintbuddy.models import ActivityEntry


def login_lines(client) -> list[ActivityEntry]:
    client.app.state.activity.drain()
    with sm_of(client)() as s:
        return list(s.scalars(select(ActivityEntry).where(ActivityEntry.area == "login").order_by(ActivityEntry.id)))


def test_signed_in_and_out_with_the_address(ready_client):
    admin_client = ready_client
    admin_client.cookies.clear()
    assert sign_in(admin_client).status_code == 303
    token = csrf_of(admin_client, "/settings?tab=general")
    assert admin_client.post("/logout", data={"csrf_token": token}).status_code == 303
    found = [(line.kind, line.actor, line.address) for line in login_lines(admin_client)]
    assert found[-2:] == [("login", "user", "testclient"), ("logout", "user", "testclient")]


def test_a_failed_sign_in_keeps_neither_name_nor_password(admin_client):
    admin_client.cookies.clear()
    sign_in(admin_client, password="WRONG-PASSWORD-MARK", username="TYPED-NAME-MARK")
    sign_in(admin_client, password="WRONG-PASSWORD-MARK")
    failed = [line for line in login_lines(admin_client) if line.kind == "login_failed"]
    assert len(failed) == 1
    assert failed[0].details["count"] == 2
    assert failed[0].address == "testclient"
    with sm_of(admin_client)() as s:
        everything = repr([(r.details, r.address, r.via) for r in s.scalars(select(ActivityEntry))])
    assert "MARK" not in everything


def test_a_wrong_password_before_a_new_one_counts_as_failed(ready_client):
    token = csrf_of(ready_client, "/settings?tab=account")
    ready_client.post(
        "/settings/password",
        data={"csrf_token": token, "current_password": "WRONG-1", "new_password": "x" * 12, "new_password_repeat": "x" * 12},
    )
    assert [line.kind for line in login_lines(ready_client)][-1] == "login_failed"


def test_a_new_password_is_logged_with_the_address(ready_client):
    token = csrf_of(ready_client, "/settings?tab=account")
    new = "another-password-2"
    response = ready_client.post(
        "/settings/password",
        data={"csrf_token": token, "current_password": PASSWORD, "new_password": new, "new_password_repeat": new},
    )
    assert response.status_code == 303
    line = login_lines(ready_client)[-1]
    assert (line.kind, line.actor, line.address) == ("credentials_changed", "user", "testclient")
    with sm_of(ready_client)() as s:
        assert new not in repr([r.details for r in s.scalars(select(ActivityEntry))])
