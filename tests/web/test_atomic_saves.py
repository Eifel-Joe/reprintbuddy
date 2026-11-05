"""Each save is one transaction: a failure half-way leaves nothing behind."""

import pytest
from conftest import ADMIN, PASSWORD, csrf_of, make_client
from sqlalchemy import event

from reprintbuddy import auth
from reprintbuddy import settings as st

OLD_URL = "http://bambuddy.test:8000"
CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
KEY_PEM = "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"


def fail_when_saving(monkeypatch, key):
    """Make set_setting blow up for one key, after the earlier ones were written."""
    real = st.set_setting

    def wrapper(session, setting_key, value, **kwargs):
        if setting_key == key:
            raise RuntimeError("simulated failure")
        return real(session, setting_key, value, **kwargs)

    monkeypatch.setattr(st, "set_setting", wrapper)


def saved(client, key):
    with client.app.state.sessionmaker() as s:
        return st.get_setting(s, key)


class Commits:
    def __init__(self, engine):
        self.count = 0
        event.listen(engine, "commit", lambda *a: setattr(self, "count", self.count + 1))


def bambuddy_form(token, **over):
    data = {
        "csrf_token": token,
        "bambuddy_url": "http://other.test:8000",
        "bambuddy_username": "other-user",
        "bambuddy_api_key": "bb_for_other",
        "bambuddy_review_confirmed": "on",
    }
    data.update(over)
    return data


def test_settings_bambuddy_failure_half_way_changes_nothing(ready_client, respx_mock, monkeypatch):
    respx_mock.get("http://other.test:8000/api/v1/queue/").respond(200, json=[])
    token = csrf_of(ready_client, "/settings")
    fail_when_saving(monkeypatch, "bambuddy_api_key")  # url and user name come first
    with pytest.raises(RuntimeError):
        ready_client.post("/settings/bambuddy", data=bambuddy_form(token))
    monkeypatch.undo()
    assert saved(ready_client, "bambuddy_url") == OLD_URL  # not "new address plus old key"
    assert saved(ready_client, "bambuddy_username") == "reprintbuddy"
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"


def test_settings_bambuddy_save_is_a_single_commit(ready_client, respx_mock):
    respx_mock.get("http://other.test:8000/api/v1/queue/").respond(200, json=[])
    token = csrf_of(ready_client, "/settings")
    commits = Commits(ready_client.app.state.engine)
    response = ready_client.post("/settings/bambuddy", data=bambuddy_form(token))
    assert response.status_code == 303
    assert commits.count == 1
    assert saved(ready_client, "bambuddy_url") == "http://other.test:8000"


def test_settings_server_failure_half_way_changes_nothing(ready_client, monkeypatch, tls_pair):
    CERT, KEY_PEM = tls_pair
    token = csrf_of(ready_client, "/settings")
    fail_when_saving(monkeypatch, "tls_key")  # host, port and certificate come first
    with pytest.raises(RuntimeError):
        ready_client.post(
            "/settings/server",
            data={"csrf_token": token, "host": "127.0.0.1", "port": "9090", "tls_cert": CERT, "tls_key": KEY_PEM},
        )
    monkeypatch.undo()
    assert saved(ready_client, "host") == "0.0.0.0"
    assert saved(ready_client, "port") == 8080
    assert saved(ready_client, "tls_cert") is None  # no half a TLS pair


def test_settings_server_save_is_a_single_commit(ready_client, tls_pair):
    CERT, KEY_PEM = tls_pair
    token = csrf_of(ready_client, "/settings")
    commits = Commits(ready_client.app.state.engine)
    response = ready_client.post(
        "/settings/server",
        data={"csrf_token": token, "host": "127.0.0.1", "port": "9090", "tls_cert": CERT, "tls_key": KEY_PEM},
    )
    assert response.status_code == 303
    assert commits.count == 1


def test_setup_bambuddy_failure_half_way_changes_nothing(admin_client, respx_mock, monkeypatch):
    respx_mock.get("http://other.test:8000/api/v1/queue/").respond(200, json=[])
    token = csrf_of(admin_client, "/setup/bambuddy")
    fail_when_saving(monkeypatch, "bambuddy_review_confirmed")  # the last of the four
    with pytest.raises(RuntimeError):
        admin_client.post("/setup/bambuddy", data=bambuddy_form(token))
    monkeypatch.undo()
    assert saved(admin_client, "bambuddy_url") is None
    assert saved(admin_client, "bambuddy_api_key") is None


def test_setup_bambuddy_save_is_a_single_commit(admin_client, respx_mock):
    respx_mock.get("http://other.test:8000/api/v1/queue/").respond(200, json=[])
    token = csrf_of(admin_client, "/setup/bambuddy")
    commits = Commits(admin_client.app.state.engine)
    response = admin_client.post("/setup/bambuddy", data=bambuddy_form(token))
    assert response.status_code == 303
    assert commits.count == 1


def admin_form(token):
    return {
        "csrf_token": token,
        "username": ADMIN,
        "password": PASSWORD,
        "password_repeat": PASSWORD,
        "language": "de",
    }


def test_setup_admin_failure_after_the_account_leaves_no_account(db_path, monkeypatch):
    client = make_client(db_path)
    token = csrf_of(client, "/setup")
    fail_when_saving(monkeypatch, "language")  # after the account was added
    with pytest.raises(RuntimeError):
        client.post("/setup", data=admin_form(token))
    monkeypatch.undo()
    with client.app.state.sessionmaker() as s:
        assert not auth.has_user(s), "an account without its language setting must not exist"
    assert client.get("/setup").status_code == 200  # setup is still open


def test_setup_admin_is_a_single_commit(db_path):
    client = make_client(db_path)
    token = csrf_of(client, "/setup")
    commits = Commits(client.app.state.engine)
    response = client.post("/setup", data=admin_form(token))
    assert response.status_code == 303
    assert commits.count == 1
