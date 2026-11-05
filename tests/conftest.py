import re

import pytest
from fastapi.testclient import TestClient

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.web.app import create_app

ADMIN = "admin"
PASSWORD = "password123"

_TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_of(client: TestClient, path: str) -> str:
    """Fetch a page and return the CSRF token from its form."""
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    match = _TOKEN.search(response.text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def api_routes(app):
    """All path operations, also those hidden in included routers (FastAPI >= 0.16x)."""
    from fastapi.routing import APIRoute

    def walk(routes):
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                yield from walk(nested.routes)
            elif isinstance(route, APIRoute):
                yield route

    return list(walk(app.routes))


def make_client(db_path) -> TestClient:
    app = create_app(db_path)
    app.state.login_failure_delay = 0  # tests do not wait; some tests set these themselves
    app.state.login_brake.delay = 0
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "t.db"


@pytest.fixture
def client(db_path):
    return make_client(db_path)


@pytest.fixture
def admin_client(client):
    """First-run step 1 done: admin account exists and is logged in."""
    token = csrf_of(client, "/setup")
    response = client.post(
        "/setup",
        data={
            "csrf_token": token,
            "username": ADMIN,
            "password": PASSWORD,
            "password_repeat": PASSWORD,
            "language": "en",
        },
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def ready_client(admin_client):
    """Step 2 done as well: Bambuddy settings written straight into the DB."""
    with admin_client.app.state.sessionmaker() as session:
        st.set_setting(session, "bambuddy_url", "http://bambuddy.test:8000")
        st.set_setting(session, "bambuddy_username", "reprintbuddy")
        st.set_setting(session, "bambuddy_api_key", "bb_testkey")
        st.set_setting(session, "bambuddy_review_confirmed", True)
    return admin_client


@pytest.fixture
def db(tmp_path):
    """A fresh migrated database; yields a sessionmaker (use `with db.begin() as s:`)."""
    engine = make_engine(tmp_path / "domain.db")
    upgrade(engine)
    yield make_sessionmaker(engine)
    engine.dispose()
