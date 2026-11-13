"""Login: password hashing off the event loop, delay after failures, trimmed names,
and the minimum password length taken from one constant."""

import asyncio
import time

import pytest
from conftest import PASSWORD, csrf_of, login, make_client

from reprintbuddy import auth


@pytest.fixture
def logged_out(admin_client):
    admin_client.cookies.clear()
    return admin_client


def test_username_is_trimmed_like_in_the_setup(logged_out):
    assert login(logged_out, username="  admin ").status_code == 303


def test_a_failed_login_is_delayed(logged_out):
    logged_out.app.state.login_failure_delay = 0.3
    started = time.monotonic()
    response = login(logged_out, password="wrong-password")
    assert response.status_code == 200
    assert time.monotonic() - started >= 0.3


def test_a_successful_login_is_not_delayed(logged_out):
    logged_out.app.state.login_failure_delay = 5.0
    started = time.monotonic()
    assert login(logged_out).status_code == 303
    assert time.monotonic() - started < 4.0


def test_the_delay_has_a_default(db_path):
    from reprintbuddy.web.app import create_app

    assert create_app(db_path).state.login_failure_delay > 0


def _record_where_hashing_runs(monkeypatch):
    """Wrap the password functions: note whether an event loop runs in the calling thread."""
    seen = []
    for name in ("verify_password", "hash_password"):
        original = getattr(auth, name)

        def wrapper(*args, _original=original, _name=name, **kwargs):
            try:
                asyncio.get_running_loop()
                seen.append((_name, "on the event loop"))
            except RuntimeError:
                seen.append((_name, "in a worker thread"))
            return _original(*args, **kwargs)

        monkeypatch.setattr(auth, name, wrapper)
    return seen


def test_login_hashing_runs_in_a_worker_thread(logged_out, monkeypatch):
    seen = _record_where_hashing_runs(monkeypatch)
    login(logged_out)
    assert ("verify_password", "in a worker thread") in seen
    assert not [s for s in seen if s[1] == "on the event loop"], seen


def test_unknown_user_costs_the_same_in_a_worker_thread(logged_out, monkeypatch):
    seen = _record_where_hashing_runs(monkeypatch)
    login(logged_out, username="nobody")
    assert seen and all(where == "in a worker thread" for _, where in seen), seen


def test_setup_hashing_runs_in_a_worker_thread(client, monkeypatch):
    seen = _record_where_hashing_runs(monkeypatch)
    token = csrf_of(client, "/setup")
    client.post(
        "/setup",
        data={
            "csrf_token": token,
            "username": "admin",
            "password": "password123",
            "password_repeat": "password123",
            "language": "en",
        },
    )
    assert ("hash_password", "in a worker thread") in seen
    assert not [s for s in seen if s[1] == "on the event loop"], seen


def test_password_change_hashing_runs_in_a_worker_thread(ready_client, monkeypatch):
    seen = _record_where_hashing_runs(monkeypatch)
    token = csrf_of(ready_client, "/settings")
    ready_client.post(
        "/settings/password",
        data={
            "csrf_token": token,
            "current_password": PASSWORD,
            "new_password": "brand-new-pass1",
            "new_password_repeat": "brand-new-pass1",
        },
    )
    assert ("hash_password", "in a worker thread") in seen
    assert not [s for s in seen if s[1] == "on the event loop"], seen


def test_minimum_password_length_comes_from_one_constant(db_path, monkeypatch):
    monkeypatch.setattr(auth, "MIN_PASSWORD_LENGTH", 12)
    client = make_client(db_path)
    page = client.get("/setup").text
    assert 'minlength="12"' in page
    assert "at least 12 characters" in page
    token = csrf_of(client, "/setup")
    response = client.post(
        "/setup",
        data={
            "csrf_token": token,
            "username": "admin",
            "password": "only-10-chr",
            "password_repeat": "only-10-chr",
            "language": "en",
        },
    )
    assert response.status_code == 200
    assert "at least 12 characters" in response.text


def test_settings_page_uses_the_same_constant(ready_client, monkeypatch):
    monkeypatch.setattr(auth, "MIN_PASSWORD_LENGTH", 11)
    page = ready_client.get("/settings?tab=account").text
    assert 'minlength="11"' in page
    assert "at least 11 characters" in page


def test_default_minimum_is_eight(client):
    assert 'minlength="8"' in client.get("/setup").text
