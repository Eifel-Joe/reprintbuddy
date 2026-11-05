import pytest
from conftest import ADMIN, PASSWORD, csrf_of, make_client

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade


def login(client, password=PASSWORD, token=None, **extra):
    token = token if token is not None else csrf_of(client, "/login")
    data = {"csrf_token": token, "username": ADMIN, "password": password}
    data.update(extra)
    return client.post("/login", data=data)


@pytest.fixture
def logged_out(admin_client):
    """Account exists, but this browser is not logged in."""
    admin_client.cookies.clear()
    return admin_client


@pytest.fixture
def fresh_db(db_path):
    engine = make_engine(db_path)
    upgrade(engine)
    with make_sessionmaker(engine)() as session:
        yield session


def test_healthz_open(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_open_with_account(logged_out):
    assert logged_out.get("/healthz").status_code == 200


def test_static_is_open(logged_out):
    # there is no such file, but the gate must not redirect
    assert logged_out.get("/static/nope.css").status_code == 404


def test_redirects_to_login(logged_out):
    response = logged_out.get("/settings")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fsettings"


def test_unauthenticated_post_forbidden(logged_out):
    response = logged_out.post("/settings/language", data={})
    assert response.status_code == 403


def test_login_wrong_password(logged_out):
    response = login(logged_out, password="wrong-password")
    assert response.status_code == 200
    assert "Wrong username or password." in response.text


def test_login_ok(logged_out):
    response = login(logged_out)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    # Bambuddy is not set up in this fixture, so "logged in" shows as step 2
    assert logged_out.get("/").headers["location"] == "/setup/bambuddy"


def test_login_next_is_followed(logged_out):
    response = login(logged_out, next="/settings")
    assert response.headers["location"] == "/settings"


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example", "/\\evil"])
def test_login_next_rejects_foreign_targets(logged_out, target):
    response = login(logged_out, next=target)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_login_without_csrf_forbidden(logged_out):
    csrf_of(logged_out, "/login")
    response = login(logged_out, token="")
    assert response.status_code == 403
    assert logged_out.get("/").headers["location"].startswith("/login")


def test_login_foreign_csrf_forbidden(logged_out):
    csrf_of(logged_out, "/login")
    assert login(logged_out, token="not-the-token").status_code == 403


def test_logout_requires_csrf(admin_client):
    response = admin_client.post("/logout", data={})
    assert response.status_code == 403
    assert admin_client.get("/").headers["location"] == "/setup/bambuddy"  # still logged in


def test_logout_ends_session(admin_client):
    token = csrf_of(admin_client, "/login")  # the login page also carries a token
    response = admin_client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 303
    assert admin_client.get("/").headers["location"].startswith("/login")


def test_cookie_flags(logged_out):
    response = login(logged_out)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie


def test_cookie_secure_when_tls_configured(db_path, fresh_db):
    st.set_setting(fresh_db, "tls_cert", "-----BEGIN CERTIFICATE-----\nabc")
    st.set_setting(fresh_db, "tls_key", "-----BEGIN PRIVATE KEY-----\nabc")
    client = make_client(db_path)
    response = client.get("/setup")
    assert "secure" in response.headers["set-cookie"].lower()


def test_short_password_rejected(fresh_db):
    with pytest.raises(ValueError):
        auth.set_password(fresh_db, "admin", "short")
    assert not auth.has_user(fresh_db)


def test_password_is_hashed_and_checked(fresh_db):
    auth.set_password(fresh_db, "admin", "password123")
    user = auth.authenticate(fresh_db, "admin", "password123")
    assert user is not None
    assert "password123" not in user.password_hash
    assert auth.authenticate(fresh_db, "admin", "wrong-password") is None
    assert auth.authenticate(fresh_db, "nobody", "password123") is None


def test_set_password_keeps_a_single_account(fresh_db):
    auth.set_password(fresh_db, "admin", "password123")
    auth.set_password(fresh_db, "admin", "another-pass1")
    assert auth.authenticate(fresh_db, "admin", "password123") is None
    assert auth.authenticate(fresh_db, "admin", "another-pass1") is not None


def test_create_admin_refuses_second_account(fresh_db):
    auth.create_admin(fresh_db, "admin", "password123")
    with pytest.raises(ValueError):
        auth.create_admin(fresh_db, "other", "password456")
    assert auth.authenticate(fresh_db, "admin", "password123") is not None


def test_session_survives_restart(logged_out, db_path):
    login(logged_out)
    cookie = logged_out.cookies.get("reprintbuddy_session")
    assert cookie
    second = make_client(db_path)  # a new app object on the same database
    second.cookies.set("reprintbuddy_session", cookie)
    response = second.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/bambuddy"  # recognised as logged in


def test_session_secret_is_reused(db_path):
    make_client(db_path)
    with make_sessionmaker(make_engine(db_path))() as s:
        first = st.ensure_session_secret(s)
    make_client(db_path)
    with make_sessionmaker(make_engine(db_path))() as s:
        assert st.ensure_session_secret(s) == first


def test_cookie_is_not_secure_with_a_certificate_alone(db_path, fresh_db):
    st.set_setting(fresh_db, "tls_cert", "-----BEGIN CERTIFICATE-----\nabc")
    client = make_client(db_path)
    assert "secure" not in client.get("/setup").headers["set-cookie"].lower()
