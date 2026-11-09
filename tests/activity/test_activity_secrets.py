"""No secret in the activity log, on its page or in the export (Spec 10.7 "Nie im Protokoll", 11.5a).

Secrets with a marker go through the ways they are saved (the settings pages, a command on the
server); afterwards the marker must not appear in the log table, on the tab or in the
export. The same for an address that carries a user, a password and a key in its query.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from conftest import PASSWORD, auth_me, csrf_of, sm_of
from reprintbuddy import settings as st
from reprintbuddy.cli import main
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.models import ActivityEntry

MARK = "ZZSECRETMARK"


def everything_logged(maker) -> str:
    with maker() as s:
        return json.dumps(
            [(r.kind, r.details, r.address, r.via) for r in s.scalars(select(ActivityEntry))], default=str
        )


def test_secrets_of_the_settings_pages_never_reach_log_page_or_export(ready_client, respx_mock, tls_pair):
    cert, key_pem = tls_pair
    client = ready_client
    # Bambuddy key
    respx_mock.get("http://other.test:8000/api/v1/auth/me").respond(200, json=auth_me())
    response = client.post(
        "/settings/bambuddy",
        data={"csrf_token": csrf_of(client, "/settings"), "bambuddy_address": "http://other.test:8000",
              "bambuddy_token": f"bb_{MARK}_1"},
    )
    assert response.status_code == 303
    # HTTPS certificate and private key
    response = client.post(
        "/settings/server",
        data={"csrf_token": csrf_of(client, "/settings"), "host": "0.0.0.0", "port": "8080",
              "tls_cert": cert, "tls_key": key_pem},
    )
    assert response.status_code == 303
    # Home Assistant token
    response = client.post(
        "/settings/notify",
        data={"csrf_token": csrf_of(client, "/settings?tab=notify"), "ha_enabled": "on",
              "ha_url": "http://ha.test:8123", "ha_token": f"{MARK}.token.sig",
              "ha_service": "notify.mobile_app_phone", "ha_names": "on", "ha_event": list(st.HA_EVENTS)},
    )
    assert response.status_code == 303
    # a new password, after a wrong one
    for current in (f"wrong-{MARK}", PASSWORD):
        client.post(
            "/settings/password",
            data={"csrf_token": csrf_of(client, "/settings?tab=account"), "current_password": current,
                  "new_password": f"new-{MARK}-pw", "new_password_repeat": f"new-{MARK}-pw"},
        )
    client.app.state.activity.drain()

    logged = everything_logged(sm_of(client))
    key_body = "".join(key_pem.strip().splitlines()[1:-1])
    for secret in (MARK, key_body[:40], "bambuddy_api_key", "tls_key", "session_secret", "password_hash"):
        assert secret not in logged, secret
    assert "settings_changed" in logged and "credentials_changed" in logged and "login_failed" in logged

    page = client.get("/settings?tab=log").text
    assert MARK not in page and key_body[:40] not in page

    export = client.get("/settings/export").text
    assert MARK not in export and key_body[:40] not in export
    assert '"activity"' in export  # the log is part of the export (Spec 11.6)


def test_an_address_never_shows_user_password_or_query(db):
    with db.begin() as s:
        st.set_setting(s, "bambuddy_url", f"http://user:{MARK}@bambuddy.test:8000/p?key={MARK}#{MARK}", commit=False)
    logged = everything_logged(db)
    assert MARK not in logged
    assert "http://bambuddy.test:8000" in logged


def test_a_password_set_on_the_server_never_reaches_the_log(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    replies = iter([f"pw-{MARK}-1", f"pw-{MARK}-1"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(replies))
    assert main(["set-password", "--username", "admin", "--db", str(path)]) == 0
    engine = make_engine(Path(path))
    upgrade(engine)
    logged = everything_logged(make_sessionmaker(engine))
    engine.dispose()
    assert "account_created" in logged and MARK not in logged
