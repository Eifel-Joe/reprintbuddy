from conftest import csrf_of

from lagerbuddy import auth
from lagerbuddy import settings as st


def _form(token, **over):
    data = {
        "csrf_token": token,
        "username": "admin",
        "password": "password123",
        "password_repeat": "password123",
        "language": "de",
    }
    data.update(over)
    return data


def _has_user(client):
    with client.app.state.sessionmaker() as s:
        return auth.has_user(s)


def test_first_visit_goes_to_setup(client):
    for path in ("/", "/login", "/settings"):
        response = client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/setup", path


def test_setup_page_renders_form(client):
    response = client.get("/setup")
    assert response.status_code == 200
    assert 'name="csrf_token"' in response.text


def test_setup_creates_admin(client):
    token = csrf_of(client, "/setup")
    response = client.post("/setup", data=_form(token))
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/bambuddy"
    assert _has_user(client)
    with client.app.state.sessionmaker() as s:
        assert st.get_setting(s, "language") == "de"


def test_setup_logs_in(client):
    token = csrf_of(client, "/setup")
    client.post("/setup", data=_form(token))
    # logged in, Bambuddy not set up yet -> step 2
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/bambuddy"


def test_setup_mismatch(client):
    token = csrf_of(client, "/setup")
    response = client.post("/setup", data=_form(token, password_repeat="different1", language="en"))
    assert response.status_code == 200
    assert "The two passwords do not match." in response.text
    assert not _has_user(client)


def test_setup_too_short(client):
    token = csrf_of(client, "/setup")
    response = client.post(
        "/setup", data=_form(token, password="short", password_repeat="short", language="en")
    )
    assert response.status_code == 200
    assert "at least 8 characters" in response.text
    assert not _has_user(client)


def test_setup_username_required(client):
    token = csrf_of(client, "/setup")
    response = client.post("/setup", data=_form(token, username="  "))
    assert response.status_code == 200
    assert not _has_user(client)


def test_setup_error_uses_chosen_language(client):
    token = csrf_of(client, "/setup")
    response = client.post(
        "/setup", data=_form(token, password_repeat="different1", language="de")
    )
    assert "stimmen nicht überein" in response.text


def test_setup_requires_csrf(client):
    csrf_of(client, "/setup")  # a session exists, but we send no token
    response = client.post("/setup", data=_form(""))
    assert response.status_code == 403
    assert not _has_user(client)


def test_setup_rejects_foreign_csrf(client):
    csrf_of(client, "/setup")
    response = client.post("/setup", data=_form("not-the-token"))
    assert response.status_code == 403
    assert not _has_user(client)


def test_setup_closed_after_user(admin_client):
    assert admin_client.get("/setup").status_code == 404
    assert admin_client.post("/setup", data={}).status_code == 404
